import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import pandas as pd
import psycopg
from dotenv import load_dotenv
from flask import Flask, flash, g, redirect, render_template, request, send_file, session, url_for
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
INTERNSHIP_TERMS = re.compile(r"\b(stage|stagiaire|intern(?:ship)?|pfe)\b", re.IGNORECASE)
PROFILE_FIELDS = [
    "first_name", "last_name", "email", "phone", "city", "education",
    "school", "specialty", "internship_type", "target_city", "availability",
    "skills", "about",
]
PROFILE_DEFAULTS = {
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "city": "Marrakech",
    "education": "Bac+3",
    "school": "",
    "specialty": "",
    "internship_type": "Stage de fin d'études",
    "target_city": "Marrakech",
    "availability": "",
    "skills": "",
    "about": "",
    "cv_name": "",
}
RESIDENCE_CITIES = ["Marrakech", "Casablanca", "Rabat", "Tanger", "Fès", "Agadir", "Autre"]
EDUCATION_OPTIONS = ["Bac", "Bac+2", "Bac+3", "Bac+4", "Bac+5", "Doctorat", "Autre"]
INTERNSHIP_TYPES = [
    "Stage d'observation", "Stage d'application",
    "Stage de fin d'études", "Stage pré-embauche",
]
TARGET_CITY_OPTIONS = [
    "Indifférent", "Marrakech", "Casablanca", "Rabat", "Tanger",
    "Fès", "Agadir", "À distance",
]

app = Flask(__name__, static_folder="assets", static_url_path="/assets")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
_database_initialized = False


def database_backend() -> str:
    configured_backend = os.environ.get("DB_BACKEND", "").strip().casefold()
    if configured_backend in {"sqlite", "postgres", "postgresql"}:
        return "postgres" if configured_backend == "postgresql" else configured_backend
    if configured_backend:
        raise RuntimeError("DB_BACKEND must be 'sqlite' or 'postgres'.")

    value = os.environ.get("DATABASE_URL", "")
    if value:
        scheme = urlsplit(value).scheme
        if scheme in {"postgres", "postgresql"}:
            return "postgres"
        if scheme == "sqlite":
            return "sqlite"
        raise RuntimeError("DATABASE_URL must use a PostgreSQL or SQLite URL.")
    return "sqlite"


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required when DB_BACKEND is postgres.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("DATABASE_URL must be a valid PostgreSQL connection URL.")
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key != "pgbouncer"]
    if not any(key == "sslmode" for key, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def sqlite_database_path() -> Path:
    configured_path = os.environ.get("SQLITE_DATABASE_PATH")
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
    elif os.environ.get("DATABASE_URL", "").startswith("sqlite:"):
        path = Path(urlsplit(os.environ["DATABASE_URL"]).path)
    else:
        path = BASE_DIR / "instance" / "stagefinder.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def db_execute(connection, statement: str, parameters: tuple = ()):
    if database_backend() == "sqlite":
        statement = statement.replace("%s", "?")
    return connection.execute(statement, parameters)


def json_database_value(value: dict):
    if database_backend() == "sqlite":
        return json.dumps(value, ensure_ascii=False)
    return Jsonb(value)


@contextmanager
def get_db_connection():
    if database_backend() == "sqlite":
        connection = sqlite3.connect(sqlite_database_path())
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        yield connection


def initialize_database() -> None:
    global _database_initialized
    if _database_initialized:
        return
    with get_db_connection() as connection:
        profile_column = "TEXT NOT NULL DEFAULT '{}'" if database_backend() == "sqlite" else "JSONB NOT NULL DEFAULT '{}'::jsonb"
        saved_column = "INTEGER NOT NULL DEFAULT 0" if database_backend() == "sqlite" else "BOOLEAN NOT NULL DEFAULT FALSE"
        db_execute(
            connection,
            f"""
            CREATE TABLE IF NOT EXISTS stagefinder_users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                profile {profile_column},
                profile_saved {saved_column}
            )
            """,
        )
    _database_initialized = True


@app.before_request
def ensure_database_initialized() -> None:
    if request.endpoint != "static":
        initialize_database()


@app.before_request
def require_login_for_pages():
    protected_endpoints = {"index", "profile_page", "insights_page"}
    if request.endpoint in protected_endpoints and current_account() is None:
        requested_path = request.full_path.rstrip("?")
        return redirect(url_for("auth", next=requested_path))
    return None


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf_token() -> bool:
    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(expected) and secrets.compare_digest(submitted, expected)


def current_account() -> dict | None:
    if hasattr(g, "stagefinder_account"):
        return g.stagefinder_account
    email = session.get("user_email")
    if not email:
        g.stagefinder_account = None
        return None
    with get_db_connection() as connection:
        account = db_execute(
            connection,
            """
            SELECT email, name, password_hash, profile, profile_saved
            FROM stagefinder_users
            WHERE email = %s
            """,
            (email,),
        ).fetchone()
    if account is not None:
        g.stagefinder_account = dict(account)
        if isinstance(g.stagefinder_account["profile"], str):
            g.stagefinder_account["profile"] = json.loads(g.stagefinder_account["profile"])
    else:
        g.stagefinder_account = None
    return g.stagefinder_account


def safe_next_url(target: str | None) -> str:
    if not target or "\\" in target or any(ord(char) < 32 for char in target):
        return url_for("index")
    parsed = urlsplit(target)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or not decoded_path.startswith("/")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
    ):
        return url_for("index")
    return target


def clean_text(value: object, default: str = "") -> str:
    if value is None or pd.isna(value):
        return default
    text = str(value).strip()
    return text if text and text.casefold() != "nan" else default


@lru_cache(maxsize=16)
def read_csv_if_present(path: str) -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        return pd.DataFrame()
    return pd.read_csv(file_path, encoding="utf-8-sig")


def city_name(value: object) -> str:
    text = clean_text(value)
    if not text:
        return "Maroc / à distance"
    lowered = text.casefold()
    for city in ("Marrakech", "Marrakesh", "Casablanca", "Rabat", "Mohammedia", "Salé", "Sale"):
        if city.casefold() in lowered:
            if city == "Marrakesh":
                return "Marrakech"
            return "Salé" if city == "Sale" else city
    if lowered in {"morocco", "maroc"}:
        return "Maroc / à distance"
    return text.split(",")[0].strip() or "Maroc / à distance"


def internship_category(title: object, description: object, skills: object) -> str:
    text = f"{clean_text(title)} {clean_text(description)} {clean_text(skills)}".casefold()
    if any(term in text for term in (
        "data", "donnée", "analytics", "intelligence artificielle", "artificial intelligence",
        "machine learning", "power bi", "business intelligence", "python", "sql",
    )):
        return "Data & IA"
    if any(term in text for term in (
        "software", "développeur", "developer", "informatique", "technology", "technologie",
        "automation", "cyber",
    )):
        return "Tech & développement"
    if any(term in text for term in ("finance", "comptab", "audit", "contrôle de gestion")):
        return "Finance & gestion"
    if any(term in text for term in ("business", "commerce", "marketing", "sales", "communication")):
        return "Business & marketing"
    return "Autres domaines"


def is_internship(title: object, employment_type: object = "", seniority: object = "") -> bool:
    return (
        clean_text(employment_type).casefold() == "internship"
        or clean_text(seniority).casefold() == "internship"
        or bool(INTERNSHIP_TERMS.search(clean_text(title)))
    )


def build_internships() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    detailed_jobs = read_csv_if_present(str(DATA_DIR / "linkedin_jobs_full.csv"))
    if not detailed_jobs.empty:
        for _, row in detailed_jobs.iterrows():
            title = row.get("job_title")
            if not is_internship(title, row.get("employment_type"), row.get("seniority_level")):
                continue
            description = row.get("description", "")
            skills = row.get("skills", "")
            records.append({
                "title": clean_text(title, "Stage sans intitulé"),
                "company": clean_text(row.get("company"), "Entreprise non précisée"),
                "city": city_name(clean_text(row.get("job_location")) or row.get("search_location")),
                "posted_date": clean_text(row.get("posted_date")),
                "url": clean_text(row.get("url")),
                "description": clean_text(description),
                "skills": clean_text(skills),
                "category": internship_category(title, description, skills),
                "contract": clean_text(row.get("employment_type"), "Stage"),
                "source": "LinkedIn",
            })

    basic_jobs = read_csv_if_present(str(DATA_DIR / "linkedin_jobs.csv"))
    if not basic_jobs.empty:
        for _, row in basic_jobs.iterrows():
            title = row.get("job_title")
            if not is_internship(title):
                continue
            records.append({
                "title": clean_text(title, "Stage sans intitulé"),
                "company": clean_text(row.get("company"), "Entreprise non précisée"),
                "city": city_name(clean_text(row.get("job_location")) or row.get("search_location")),
                "posted_date": clean_text(row.get("posted_date")),
                "url": clean_text(row.get("url")),
                "description": "",
                "skills": "",
                "category": internship_category(title, "", ""),
                "contract": "Stage",
                "source": "LinkedIn",
            })

    for scrape_path in (DATA_DIR / "stagiaires_jobs.csv", BASE_DIR / "stagiaires_jobs.csv"):
        stage_jobs = read_csv_if_present(str(scrape_path))
        if stage_jobs.empty:
            continue
        for _, row in stage_jobs.iterrows():
            title = row.get("job_title")
            description = row.get("description", "")
            skills = row.get("skills", "")
            records.append({
                "title": clean_text(title, "Stage sans intitulé"),
                "company": clean_text(row.get("company"), "Entreprise non précisée"),
                "city": city_name(clean_text(row.get("city")) or row.get("job_location")),
                "posted_date": clean_text(row.get("posted_date")),
                "url": clean_text(row.get("url")),
                "description": clean_text(description),
                "skills": clean_text(skills),
                "category": internship_category(title, description, skills),
                "contract": clean_text(row.get("contract_type"), "Stage"),
                "source": "Stagiaires.ma",
            })

    internships = pd.DataFrame(records)
    if internships.empty:
        return []
    internships = internships.drop_duplicates(subset=["url", "title", "company"], keep="first")
    internships["posted_date"] = pd.to_datetime(internships["posted_date"], errors="coerce")
    internships = internships.sort_values("posted_date", ascending=False, na_position="last")

    prepared = []
    for row in internships.to_dict(orient="records"):
        description = re.sub(r"\s+", " ", clean_text(row.get("description"))).strip()
        excerpt = description[:220].rstrip()
        if len(description) > 220:
            excerpt += "…"
        posted_date = row.get("posted_date")
        date_label = posted_date.strftime("%d %b %Y") if pd.notna(posted_date) else "Date non précisée"
        url = clean_text(row.get("url"))
        if not url.startswith(("https://", "http://")):
            url = ""
        prepared.append({
            "title": clean_text(row.get("title"), "Stage sans intitulé"),
            "company": clean_text(row.get("company"), "Entreprise non précisée"),
            "city": clean_text(row.get("city"), "Maroc / à distance"),
            "url": url,
            "description": description,
            "skills": clean_text(row.get("skills")),
            "category": clean_text(row.get("category"), "Stage"),
            "contract": clean_text(row.get("contract"), "Stage"),
            "source": clean_text(row.get("source"), "Source inconnue"),
            "date_label": date_label,
            "excerpt": excerpt or "Consultez l’offre pour découvrir les missions et le profil recherché.",
        })
    return prepared


def load_businesses() -> pd.DataFrame:
    businesses = read_csv_if_present(str(DATA_DIR / "entreprises_IA_Marrakech.csv")).copy()
    if businesses.empty:
        return businesses
    if "note" in businesses:
        businesses["note"] = pd.to_numeric(businesses["note"], errors="coerce")
    else:
        businesses["note"] = pd.Series(dtype=float)
    if "categorie" not in businesses:
        businesses["categorie"] = "Autre"
    else:
        businesses["categorie"] = businesses["categorie"].fillna("Autre")
    return businesses


def completion_percentage(profile: dict) -> int:
    return round(100 * sum(bool(clean_text(profile.get(field))) for field in PROFILE_FIELDS) / len(PROFILE_FIELDS))


def site_context(active_page: str) -> dict:
    internships = build_internships()
    businesses = load_businesses()
    cities = sorted({job["city"] for job in internships})
    categories = sorted({job["category"] for job in internships})
    average_rating = businesses["note"].mean() if not businesses.empty else 0
    business_counts = (
        businesses["categorie"].value_counts().head(8).to_dict()
        if not businesses.empty else {}
    )
    account = current_account()
    profile = account["profile"] if account else PROFILE_DEFAULTS.copy()
    profile_category = internship_category(profile["specialty"], profile["about"], profile["skills"])
    chart_data = {
        "cities": pd.Series([job["city"] for job in internships]).value_counts().to_dict(),
        "categories": pd.Series([job["category"] for job in internships]).value_counts().to_dict(),
        "businesses": business_counts,
    }
    metrics = [
        (str(len(internships)), "offres de stage", "D’après les données collectées"),
        (str(len(cities)), "villes couvertes", "Au Maroc et à distance"),
        (str(len(categories)), "domaines", "Pour plusieurs profils"),
        (f"{average_rating:.1f}/5", "note moyenne locale", f"{len(businesses)} établissements référencés"),
    ]
    return {
        "internships": internships,
        "city_options": cities,
        "category_options": categories,
        "businesses_count": len(businesses),
        "average_rating": average_rating,
        "business_counts": business_counts,
        "chart_data": chart_data,
        "metrics": metrics,
        "profile": profile,
        "has_profile": bool(account and account.get("profile_saved")),
        "profile_completion": completion_percentage(profile) if account and account.get("profile_saved") else 0,
        "profile_category": profile_category,
        "current_user": account,
        "csrf_token": csrf_token(),
        "residence_cities": RESIDENCE_CITIES,
        "education_options": EDUCATION_OPTIONS,
        "internship_types": INTERNSHIP_TYPES,
        "target_city_options": TARGET_CITY_OPTIONS,
        "active_page": active_page,
    }


@app.get("/")
def index():
    context = site_context("offers")
    return render_template("index.html", **context)


@app.get("/profile")
@app.get("/stagiaire/profil")
def profile_page():
    context = site_context("profile")
    return render_template("profile.html", **context)


@app.get("/insights")
def insights_page():
    context = site_context("insights")
    return render_template("insights.html", **context)


@app.get("/auth")
def auth():
    if current_account():
        return redirect(url_for("index"))
    return render_template(
        "auth.html",
        csrf_token=csrf_token(),
        next_url=safe_next_url(request.args.get("next")),
    )


@app.post("/signup")
def signup():
    next_url = safe_next_url(request.form.get("next"))
    if not valid_csrf_token():
        flash("La session a expiré. Réessaie.", "error")
        return redirect(url_for("auth", next=next_url))

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    confirmation = request.form.get("confirm_password", "")

    if len(name) < 2:
        flash("Saisis ton nom pour créer un compte.", "error")
    elif re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
        flash("Saisis une adresse e-mail valide.", "error")
    elif len(password) < 8:
        flash("Le mot de passe doit contenir au moins 8 caractères.", "error")
    elif password != confirmation:
        flash("Les mots de passe ne correspondent pas.", "error")
    else:
        first_name, _, last_name = name.partition(" ")
        profile = PROFILE_DEFAULTS.copy()
        profile.update({"first_name": first_name, "last_name": last_name, "email": email})
        try:
            with get_db_connection() as connection:
                db_execute(
                    connection,
                    """
                    INSERT INTO stagefinder_users (email, name, password_hash, profile)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (email, name, generate_password_hash(password), json_database_value(profile)),
                )
        except (UniqueViolation, sqlite3.IntegrityError):
            flash("Un compte existe déjà avec cette adresse e-mail.", "error")
            return redirect(url_for("auth", next=next_url) + "#signup")
        session.clear()
        session["user_email"] = email
        csrf_token()
        flash(f"Bienvenue sur StageFinder, {first_name} !", "success")
        return redirect(next_url)

    return redirect(url_for("auth", next=next_url) + "#signup")


@app.post("/login")
def login():
    next_url = safe_next_url(request.form.get("next"))
    if not valid_csrf_token():
        flash("La session a expiré. Réessaie.", "error")
        return redirect(url_for("auth", next=next_url))

    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    with get_db_connection() as connection:
        account = db_execute(
            connection,
            """
            SELECT email, name, password_hash, profile, profile_saved
            FROM stagefinder_users
            WHERE email = %s
            """,
            (email,),
        ).fetchone()
    if account is None or not check_password_hash(account["password_hash"], password):
        flash("Adresse e-mail ou mot de passe incorrect.", "error")
        return redirect(url_for("auth", next=next_url) + "#login")

    session.clear()
    session["user_email"] = email
    csrf_token()
    flash(f"Content de te revoir, {account['name']} !", "success")
    return redirect(next_url)


@app.post("/logout")
def logout():
    if not valid_csrf_token():
        flash("La session a expiré. Réessaie.", "error")
        return redirect(url_for("auth"))
    session.clear()
    flash("Tu es déconnecté(e).", "success")
    return redirect(url_for("auth"))


@app.post("/profile")
def save_profile():
    account = current_account()
    if account is None:
        flash("Connecte-toi pour enregistrer ton profil.", "error")
        return redirect(url_for("auth"))
    if not valid_csrf_token():
        flash("La session a expiré. Réessaie.", "error")
        return redirect(url_for("profile_page"))

    form = request.form
    profile = {
        "first_name": form.get("first_name", "").strip(),
        "last_name": form.get("last_name", "").strip(),
        "email": form.get("email", "").strip(),
        "phone": form.get("phone", "").strip(),
        "city": form.get("city", PROFILE_DEFAULTS["city"]),
        "education": form.get("education", PROFILE_DEFAULTS["education"]),
        "school": form.get("school", "").strip(),
        "specialty": form.get("specialty", "").strip(),
        "internship_type": form.get("internship_type", PROFILE_DEFAULTS["internship_type"]),
        "target_city": form.get("target_city", PROFILE_DEFAULTS["target_city"]),
        "availability": form.get("availability", "").strip(),
        "skills": form.get("skills", "").strip(),
        "about": form.get("about", "").strip(),
        "cv_name": account["profile"].get("cv_name", ""),
    }
    uploaded_cv = request.files.get("cv_file")
    if uploaded_cv and uploaded_cv.filename:
        extension = Path(uploaded_cv.filename).suffix.casefold()
        if extension not in {".pdf", ".doc", ".docx"}:
            flash("Le CV doit être au format PDF, DOC ou DOCX.", "error")
            return redirect(url_for("profile_page"))
        profile["cv_name"] = Path(uploaded_cv.filename).name

    if not profile["first_name"] or not profile["last_name"]:
        flash("Renseigne ton prénom et ton nom pour créer ton profil.", "error")
    elif re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", profile["email"]) is None:
        flash("Saisis une adresse e-mail valide.", "error")
    else:
        with get_db_connection() as connection:
            db_execute(
                connection,
                """
                UPDATE stagefinder_users
                SET profile = %s, profile_saved = TRUE
                WHERE email = %s
                """,
                (json_database_value(profile), account["email"]),
            )
        flash(f"Ton profil est prêt, {profile['first_name']} ! Il reste disponible dans cette session.", "success")
    return redirect(url_for("profile_page"))


@app.get("/profile.json")
def download_profile():
    account = current_account()
    if account is None:
        flash("Connecte-toi pour télécharger ton profil.", "error")
        return redirect(url_for("auth"))
    if not account.get("profile_saved"):
        flash("Enregistre ton profil avant de le télécharger.", "error")
        return redirect(url_for("profile_page"))
    profile = account["profile"]
    contents = json.dumps(profile, ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        BytesIO(contents),
        mimetype="application/json",
        as_attachment=True,
        download_name="mon-profil-stagefinder.json",
    )


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
