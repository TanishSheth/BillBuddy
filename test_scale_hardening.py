"""Regression checks for scale-hardening phases."""
import os
import uuid

os.environ.setdefault("BILLBUDDY_ENV", "development")

from sqlalchemy.orm import selectinload

from app import models
from app.config import ASYNC_OCR, RUN_OCR_POLLER, RUN_SCHEDULER
from app.database import Base, SessionLocal, engine
from app.main import app
from app.schemas import FriendsConsolidatedSummaryOut, ScanJobOut
from app.scan_worker import _claim_job, content_fingerprint, fingerprint_key
from fastapi.testclient import TestClient


def test_config_defaults_for_dev():
    assert RUN_SCHEDULER is True
    assert ASYNC_OCR is True
    assert RUN_OCR_POLLER is True


def test_fingerprint_key_stable():
    a = fingerprint_key({"phones": ["1"], "header_key": "x"})
    b = fingerprint_key({"header_key": "x", "phones": ["1"]})
    assert a == b
    assert len(a) == 64
    assert content_fingerprint(b"abc") == content_fingerprint(b"abc")
    assert content_fingerprint(b"abc") != content_fingerprint(b"abd")


def test_health_and_ready_endpoints():
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert "async_ocr" in health


def test_summary_and_scan_schemas_exist():
    assert FriendsConsolidatedSummaryOut.__name__
    assert ScanJobOut.__name__


def test_expense_relationships_support_selectinload():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Empty query must accept selectinload options used by balance engine.
        rows = (
            db.query(models.Expense)
            .options(
                selectinload(models.Expense.payers),
                selectinload(models.Expense.splits),
            )
            .limit(1)
            .all()
        )
        assert isinstance(rows, list)
    finally:
        db.close()


def test_scan_job_atomic_claim():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        account = db.query(models.Account).first()
        if not account:
            from app.auth import hash_password
            account = models.Account(
                email=f"scale-claim-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("Password12!"),
                display_name="Scale",
            )
            db.add(account)
            db.commit()
            db.refresh(account)
        job_id = uuid.uuid4().hex
        db.add(
            models.ScanJob(
                id=job_id,
                account_id=account.id,
                upload_id=uuid.uuid4().hex,
                file_path="/tmp/missing.png",
                mime_type="image/png",
                status="queued",
            )
        )
        db.commit()
        claimed = _claim_job(db, job_id)
        assert claimed is not None
        assert claimed.status == "processing"
        # Second claim must not steal a processing job into a new race.
        again = _claim_job(db, job_id)
        assert again is not None
        assert again.status == "processing"
    finally:
        db.close()


def test_compose_defines_scan_worker():
    compose = open(
        os.path.join(os.path.dirname(__file__), "docker-compose.yml"),
        encoding="utf-8",
    ).read()
    assert "scan-worker:" in compose
    assert "RUN_OCR_POLLER" in compose
    assert "app.scan_worker_main" in compose
