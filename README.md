# StageFinder

## Deploy on PythonAnywhere

1. Create a PythonAnywhere account and a new **Manual configuration** web app using the same Python version as your virtual environment supports.
2. Upload or clone this project to `/home/<your-username>/StageFinder`. Do not upload `.env` or `.venv`.
3. In a Bash console, create a virtual environment and install dependencies:

   ```bash
   cd ~/StageFinder
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   Replace `python3.12` with the Python version selected for the web app.
4. Open the web app's WSGI configuration file and add this block, replacing the project path and the placeholder values:

   ```python
   import os
   import sys

   project_home = "/home/<your-username>/StageFinder"
   if project_home not in sys.path:
       sys.path.insert(0, project_home)

   os.environ["DB_BACKEND"] = "sqlite"
   os.environ["SQLITE_DATABASE_PATH"] = "/home/<your-username>/StageFinder/instance/stagefinder.sqlite3"
   os.environ["FLASK_SECRET_KEY"] = "a long random secret"

   from wsgi import application
   ```

   Enter the real secret only in PythonAnywhere's WSGI configuration; never commit it to this repository. The SQLite database is stored on your PythonAnywhere home directory and persists across app reloads. The included `wsgi.py` sets the project import path, checks that the secret is configured, and enables secure session cookies for HTTPS.
5. In the **Web** tab, set the virtualenv path to `/home/<your-username>/StageFinder/.venv`, configure static files with URL `/assets/` and path `/home/<your-username>/StageFinder/assets/`, then click **Reload**.

The app creates its user table on the first database request. After deployment, visit the site and create an account.

## Run locally

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`. SQLite is the default and stores data in `instance/stagefinder.sqlite3`; set `FLASK_SECRET_KEY` to a long random value.
3. Start the app: `python main.py`
4. Open `http://127.0.0.1:5000`.

The app creates the `stagefinder_users` table on its first database request. Accounts and profiles are stored in SQLite by default. Passwords are saved as secure password hashes, not plaintext. PostgreSQL remains available by setting `DB_BACKEND=postgres` and `DATABASE_URL`. Do not commit `.env` or the SQLite database file; both are ignored by Git.
