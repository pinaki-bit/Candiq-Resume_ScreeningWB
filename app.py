import os
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).parent / "backend"
sys.path.append(str(backend_dir))

# Ensure data directory for SQLite exists
# Hugging Face runs in a readonly environment except for specific dirs, but for free tier we can write to local tmp or current dir
os.environ["DATABASE_URL"] = "sqlite:///./resume_screening.db"
os.environ["UPLOAD_DIR"] = "./backend/uploads"
os.makedirs("./backend/uploads", exist_ok=True)

import uvicorn
from backend.app.main import app as fastapi_app

if __name__ == "__main__":
    uvicorn.run(fastapi_app, host="0.0.0.0", port=7860)
