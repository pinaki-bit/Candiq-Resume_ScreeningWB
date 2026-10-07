import os
import sys
from pathlib import Path

# Add the root directory to the python path so it can import the backend package
sys.path.append(str(Path(__file__).parent.parent))

from backend.app.main import app
