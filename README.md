# StageFinder Maroc

## Run locally

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set `DATABASE_URL` to the Supabase transaction-pooler URL and `FLASK_SECRET_KEY` to a long random value. `DIRECT_URL` is only needed by migration tools and is not used by this Flask app.
3. Start the app: `python main.py`
4. Open `http://127.0.0.1:5000`.

The app creates the `stagefinder_users` table on its first database request. Sign-up accounts, password hashes, and student profiles are stored in PostgreSQL. Passwords are hashed before storage. Do not commit `.env` or share its contents. If database credentials have been exposed, rotate the database password in Supabase and update `.env`.
