import os
import sys
from pathlib import Path

# Add the root directory to the python path so it can import the backend package
sys.path.append(str(Path(__file__).parent.parent))

# Vercel serverless environment is read-only except for /tmp.
# Since we are using SQLite, we must store the DB in /tmp to avoid OperationalError.
os.environ["DATABASE_URL"] = "sqlite:////tmp/resume_screening.db"
os.environ["UPLOAD_DIR"] = "/tmp/uploads"

from backend.app.main import app
