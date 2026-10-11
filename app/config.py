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


APP_VERSION = os.environ.get("APP_VERSION", "20261011preferVlm")
ENVIRONMENT = os.environ.get("BILLBUDDY_ENV", "development").strip().lower()
IS_DEVELOPMENT = ENVIRONMENT == "development"
DISABLE_SCHEDULER = _is_truthy(os.environ.get("DISABLE_SCHEDULER"))
# Exactly one process should run scheduled jobs in multi-worker deploys.
RUN_SCHEDULER = _is_truthy(
    os.environ.get("RUN_SCHEDULER", "true" if IS_DEVELOPMENT else "false")
)
# OCR off the request path by default; set SYNC_OCR=1 for emergency fallback.
ASYNC_OCR = not _is_truthy(os.environ.get("SYNC_OCR"))
OCR_WORKER_CONCURRENCY = max(1, int(os.environ.get("OCR_WORKER_CONCURRENCY", "1")))
# When false, this process will not run the scan poller (use a dedicated scan-worker).
RUN_OCR_POLLER = _is_truthy(os.environ.get("RUN_OCR_POLLER", "true" if IS_DEVELOPMENT else "true"))
TMP_UPLOAD_TTL_HOURS = int(os.environ.get("TMP_UPLOAD_TTL_HOURS", "24"))

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

REDIS_URL = (os.environ.get("REDIS_URL") or "").strip() or None
STORAGE_BACKEND = (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()
S3_BUCKET = (os.environ.get("S3_BUCKET") or "").strip() or None
S3_ENDPOINT_URL = (os.environ.get("S3_ENDPOINT_URL") or "").strip() or None
S3_ACCESS_KEY = (os.environ.get("S3_ACCESS_KEY") or "").strip() or None
S3_SECRET_KEY = (os.environ.get("S3_SECRET_KEY") or "").strip() or None
S3_REGION = (os.environ.get("S3_REGION") or "auto").strip() or "auto"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]


def _validate_production_configuration() -> None:
    """Fail closed when a production container is missing its security basics."""
    if ENVIRONMENT != "production":
        return
    errors: list[str] = []
    if not SECRET_KEY or SECRET_KEY == "dev-only-change-me-in-production" or len(SECRET_KEY) < 32:
        errors.append("SECRET_KEY must be a unique value of at least 32 characters")
    if not BASE_URL.startswith("https://"):
        errors.append("BASE_URL must use https://")
    if DATABASE_URL.startswith("sqlite:"):
        errors.append("DATABASE_URL must use PostgreSQL in production")
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))


_validate_production_configuration()
