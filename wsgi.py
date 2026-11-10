import os
import sys
from pathlib import Path


PROJECT_HOME = Path(__file__).resolve().parent
project_path = str(PROJECT_HOME)
if project_path not in sys.path:
    sys.path.insert(0, project_path)

missing_settings = [
    name for name in ("FLASK_SECRET_KEY",)
    if not os.environ.get(name)
]
if missing_settings:
    raise RuntimeError(
        "Set the following environment variables in the hosting WSGI configuration: "
        + ", ".join(missing_settings)
    )

from main import app as application

application.config["SESSION_COOKIE_SECURE"] = True
