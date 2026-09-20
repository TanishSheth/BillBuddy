"""Environment and deployment configuration."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


APP_VERSION = os.environ.get("APP_VERSION", "20260920c")
IS_DEVELOPMENT = os.environ.get("BILLBUDDY_ENV", "").lower() == "development"
DISABLE_SCHEDULER = _is_truthy(os.environ.get("DISABLE_SCHEDULER"))

DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
TMP_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "tmp")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

DB_PATH = os.path.join(DATA_DIR, "billbuddy.db")
DATABASE_URL = _normalize_database_url(
    os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8001")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
ACCESS_TOKEN_EXPIRE_DAYS = int(os.environ.get("ACCESS_TOKEN_EXPIRE_DAYS", "7"))

PORT = int(os.environ.get("PORT", "8000"))
