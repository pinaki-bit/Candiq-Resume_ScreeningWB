import os
import sys
from pathlib import Path

# Add the root directory to the python path so it can import the backend package
sys.path.append(str(Path(__file__).parent.parent))

# Vercel serverless environment is read-only except for /tmp.
# Since we are using SQLite, we store the DB in /tmp to allow write operations.
import shutil

repo_root = Path(__file__).parent.parent
db_source = repo_root / "resume_screening.db"
db_target = Path("/tmp/resume_screening.db")
uploads_target = Path("/tmp/uploads")
uploads_target.mkdir(parents=True, exist_ok=True)

if db_source.exists() and not db_target.exists():
    try:
        shutil.copyfile(db_source, db_target)
    except Exception:
        pass

os.environ["DATABASE_URL"] = "sqlite:////tmp/resume_screening.db"
os.environ["UPLOAD_DIR"] = "/tmp/uploads"
os.environ.setdefault("SECRET_KEY", "candiq-production-secret-jwt-key-2026-auth-secure")

from backend.app.main import app, _init_db
from backend.app.config import get_settings

# Guarantee DB tables & seed admin even if ASGI lifespan is skipped by serverless bridge
try:
    _init_db(get_settings())
except Exception as e:
    import logging
    logging.getLogger("api.index").warning("Failed to run _init_db on cold start: %s", e)
