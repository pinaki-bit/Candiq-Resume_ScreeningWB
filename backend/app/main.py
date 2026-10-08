"""
backend/app/main.py

FastAPI application factory — v0.3.0

Security enhancements:
  - CORS restricted to specific methods/headers (no wildcards)
  - API docs conditionally disabled in production
  - Startup security checks (refuses insecure production configs)
  - Rate limiter registered for login/upload enforcement
  - Default admin seeded with must_change_password=True
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings, startup_security_checks
from app.core.security import hash_password
from app.database import create_all_tables, get_db
from app.models.user import User
from app.rate_limiter import limiter

# Routers — new v1 namespace
from app.api.v1 import auth as auth_v1
from app.api.v1 import jobs as jobs_v1
from app.api.v1 import resumes as resumes_v1
from app.api.v1 import screening as screening_v1
from app.api.v1 import admin as admin_v1
from app.api.v1 import analytics as analytics_v1
from app.api.v1 import ats as ats_v1
from app.api.v1 import extension as extension_v1
from app.api.v1 import ai as ai_v1
from app.api.v1 import resume_builder as resume_builder_v1
from app.api.v1 import discovery as discovery_v1
from app.api.v1 import pipeline as pipeline_v1
from app.api.v1 import compare as compare_v1

# Legacy routers (kept for backward compat during migration)
from app.routers import health

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()

    # --- Security checks before anything else ---
    startup_security_checks(settings)

    logger.info("Starting %s v%s [%s]", settings.app_title, settings.app_version, settings.app_env)
    _init_db(settings)
    yield
    logger.info("Shutting down %s.", settings.app_title)
    from app.database import engine
    engine.dispose()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        description=(
            "Candiq — Candidate Intelligence & AI-Powered Recruitment Platform\n\n"
            "**Phase 1–3**: Authentication, health checks, job management, "
            "NLP/ML foundation.\n"
            "**Phase 4+**: Resume upload, screening, analytics, admin dashboard."
        ),
        docs_url=settings.docs_url,
        redoc_url=settings.redoc_url,
        openapi_url=settings.openapi_url,
        lifespan=_lifespan,
    )

    # ── Rate limiter state ──────────────────────────────────────────────────
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Global Structured Exception Handler ─────────────────────────────────
    @application.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        import datetime
        req_id = getattr(request.state, "request_id", "unknown")
        logger.error("Unhandled exception [req_id=%s]: %s", req_id, exc, exc_info=not settings.is_production)
        detail = "An internal server error occurred." if settings.is_production else str(exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": detail,
                "error_code": "INTERNAL_SERVER_ERROR",
                "request_id": req_id,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )

    # ── Request Correlation & Latency Observability Middleware ───────────────
    @application.middleware("http")
    async def request_correlation_and_metrics_middleware(request: Request, call_next):
        import time
        import uuid
        from app.core.metrics import metrics_collector

        req_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
        request.state.request_id = req_id

        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000.0

        metrics_collector.record_request(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        response.headers["X-Request-ID"] = req_id
        return response

    # ── CORS — restricted methods and headers (no wildcards) ────────────────
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"^https://.*\.vercel\.app$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID", "X-Requested-With"],
    )

    # ── Security headers middleware ─────────────────────────────────────────
    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response

    # ── v1 API routers ──────────────────────────────────────────────────────
    application.include_router(auth_v1.router)
    application.include_router(jobs_v1.router)
    application.include_router(resumes_v1.router)
    application.include_router(screening_v1.router)
    application.include_router(admin_v1.router)
    application.include_router(analytics_v1.router)
    application.include_router(ats_v1.router)
    application.include_router(extension_v1.router)
    application.include_router(ai_v1.router)
    application.include_router(resume_builder_v1.router)
    application.include_router(discovery_v1.router)
    application.include_router(pipeline_v1.router)
    application.include_router(compare_v1.router)

    # ── Real-Time WebSocket Pipeline Event Endpoint ─────────────────────────
    from app.services.websocket_manager import ws_manager

    @application.websocket("/ws/pipeline")
    @application.websocket("/api/v1/ws/pipeline")
    async def websocket_pipeline_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)
        except Exception:
            ws_manager.disconnect(websocket)

    # ── Health & Observability Routers ──────────────────────────────────────
    application.include_router(health.router)
    application.include_router(health.router, prefix="/api/v1")

    return application


# ---------------------------------------------------------------------------
# DB initialization helpers
# ---------------------------------------------------------------------------

def _init_db(settings) -> None:
    """Create all tables and seed the default admin if not present."""
    create_all_tables()
    _seed_admin(settings)


def _seed_admin(settings) -> None:
    db = next(get_db())
    try:
        existing = db.query(User).filter(
            User.email == settings.admin_email.lower()
        ).first()
        if not existing:
            admin = User(
                email=settings.admin_email.lower(),
                hashed_password=hash_password(settings.admin_password),
                full_name="System Administrator",
                role="admin",
                is_active=True,
                is_admin=True,
                # Force password change if using default credentials
                must_change_password=settings.has_default_admin_password,
            )
            db.add(admin)
            db.commit()
            if settings.has_default_admin_password:
                logger.warning(
                    "⚠️  Seeded admin user '%s' with DEFAULT password. "
                    "The admin will be forced to change it on first login.",
                    settings.admin_email,
                )
            else:
                logger.info("Seeded admin user: %s", settings.admin_email)
        else:
            # Sync the admin password/role from env vars on every startup.
            # This ensures that changing ADMIN_PASSWORD in the environment
            # (e.g. on Render/Heroku) will take effect on the next deploy.
            changed = False
            new_hashed = hash_password(settings.admin_password)
            if existing.hashed_password != new_hashed:
                existing.hashed_password = new_hashed
                existing.must_change_password = settings.has_default_admin_password
                changed = True
                logger.info("🔑 Admin password synced from environment variable.")
            if existing.role != "admin":
                existing.role = "admin"
                existing.is_admin = True
                changed = True
                logger.info("🔑 Admin role corrected to 'admin'.")
            if not existing.is_active:
                existing.is_active = True
                changed = True
                logger.info("🔑 Admin account re-activated.")
            if changed:
                db.commit()
            else:
                logger.debug("Admin user already exists and is up to date.")
    except Exception as exc:
        logger.error("Failed to seed admin user: %s", exc)
        db.rollback()
    finally:
        db.close()



# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = create_app()
