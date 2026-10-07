"""DB-claimed OCR/VLM worker so receipt scans do not block API workers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import ASYNC_OCR, OCR_WORKER_CONCURRENCY, RUN_OCR_POLLER, TMP_UPLOAD_TTL_HOURS
from .database import SessionLocal
from . import models
from . import redis_support
from .vlm import extract_receipt_data

logger = logging.getLogger(__name__)

_semaphore = threading.Semaphore(OCR_WORKER_CONCURRENCY)
_worker_started = False
_worker_lock = threading.Lock()


def fingerprint_key(fingerprint: Any) -> Optional[str]:
    if not fingerprint:
        return None
    if isinstance(fingerprint, dict):
        payload = json.dumps(fingerprint, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]
    return str(fingerprint)[:64]


def content_fingerprint(contents: bytes) -> str:
    """Stable SHA-256 of upload bytes for immediate scan dedup."""
    return hashlib.sha256(contents or b"").hexdigest()[:64]


def _learned_vendors(db, account_id: int) -> list[dict]:
    rows = (
        db.query(models.AccountVendor)
        .filter_by(account_id=account_id)
        .order_by(models.AccountVendor.id.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "name": row.name,
            "phones": {row.phone} if row.phone else set(),
            "store_ids": {row.store_id} if row.store_id else set(),
            "header_key": row.header_key or "",
            "category": row.category or "General",
        }
        for row in rows
    ]


def enqueue_scan_job(
    *,
    job_id: str,
    account_id: int,
    group_id: Optional[int],
    upload_id: str,
    file_path: str,
    mime_type: str,
    image_url: str,
    fingerprint: Optional[str] = None,
) -> models.ScanJob:
    db = SessionLocal()
    try:
        if fingerprint:
            existing = (
                db.query(models.ScanJob)
                .filter_by(account_id=account_id, fingerprint=fingerprint, status="succeeded")
                .order_by(models.ScanJob.created_at.desc())
                .first()
            )
            if existing and existing.result_json:
                job = models.ScanJob(
                    id=job_id,
                    account_id=account_id,
                    group_id=group_id,
                    upload_id=upload_id,
                    file_path=file_path,
                    mime_type=mime_type,
                    status="succeeded",
                    fingerprint=fingerprint,
                    result_json=existing.result_json,
                    source=existing.source,
                    message="Reused previous scan for the same receipt.",
                    image_url=image_url,
                    finished_at=datetime.utcnow(),
                )
                db.add(job)
                db.commit()
                db.refresh(job)
                return job

        job = models.ScanJob(
            id=job_id,
            account_id=account_id,
            group_id=group_id,
            upload_id=upload_id,
            file_path=file_path,
            mime_type=mime_type,
            status="queued",
            fingerprint=fingerprint,
            image_url=image_url,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


def _claim_job(db, job_id: str) -> Optional[models.ScanJob]:
    """Atomically move queued → processing. Returns job or None if another worker won."""
    now = datetime.utcnow()
    updated = (
        db.query(models.ScanJob)
        .filter(models.ScanJob.id == job_id, models.ScanJob.status == "queued")
        .update({"status": "processing", "claimed_at": now}, synchronize_session=False)
    )
    db.commit()
    if updated:
        return db.query(models.ScanJob).filter_by(id=job_id).first()
    job = db.query(models.ScanJob).filter_by(id=job_id).first()
    if job and job.status == "processing":
        return job
    return None


def process_scan_job(job_id: str, vlm_config) -> None:
    """Claim and process one job. Safe to call from any worker."""
    if not _semaphore.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        job = _claim_job(db, job_id)
        if not job:
            return

        if not os.path.exists(job.file_path):
            job.status = "failed"
            job.error = "Upload file is missing"
            job.finished_at = datetime.utcnow()
            db.commit()
            return

        with open(job.file_path, "rb") as handle:
            contents = handle.read()

        extraction, source, message, fingerprint = extract_receipt_data(
            contents,
            job.mime_type,
            vlm_config,
            learned_vendors=_learned_vendors(db, job.account_id),
        )
        job.status = "succeeded"
        job.source = source
        job.message = (message or "")[:300] or None
        # Prefer store fingerprint when available; keep content hash from enqueue.
        store_fp = fingerprint_key(fingerprint)
        if store_fp:
            job.fingerprint = store_fp
        if hasattr(extraction, "model_dump"):
            job.result_json = json.dumps(extraction.model_dump())
        else:
            job.result_json = json.dumps(extraction)
        job.finished_at = datetime.utcnow()
        job.error = None

        pending = db.query(models.PendingUpload).filter_by(id=job.upload_id).first()
        if pending and isinstance(fingerprint, dict):
            pending.ocr_store_name = (getattr(extraction, "store_name", None) or "")[:150]
            pending.ocr_phones = ",".join(fingerprint.get("phones") or [])[:200]
            pending.ocr_store_ids = ",".join(fingerprint.get("store_ids") or [])[:80]
            pending.ocr_header_key = (fingerprint.get("header_key") or "")[:200]
        db.commit()
    except Exception as err:
        logger.exception("Scan job %s failed", job_id)
        try:
            job = db.query(models.ScanJob).filter_by(id=job_id).first()
            if job:
                job.status = "failed"
                job.error = str(err)[:300]
                job.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
        _semaphore.release()


def kick_scan_worker(job_id: str, vlm_config) -> None:
    if not ASYNC_OCR:
        process_scan_job(job_id, vlm_config)
        return
    thread = threading.Thread(
        target=process_scan_job,
        args=(job_id, vlm_config),
        name=f"scan-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def reclaim_stale_jobs(max_age_minutes: int = 15) -> int:
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        rows = (
            db.query(models.ScanJob)
            .filter(models.ScanJob.status == "processing", models.ScanJob.claimed_at < cutoff)
            .all()
        )
        for job in rows:
            job.status = "queued"
            job.claimed_at = None
        if rows:
            db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        logger.exception("Could not reclaim stale scan jobs")
        return 0
    finally:
        db.close()


def purge_old_tmp_uploads() -> int:
    cutoff = datetime.utcnow() - timedelta(hours=TMP_UPLOAD_TTL_HOURS)
    removed = 0
    db = SessionLocal()
    try:
        rows = db.query(models.PendingUpload).filter(models.PendingUpload.created_at < cutoff).all()
        for row in rows:
            if row.file_path and os.path.exists(row.file_path):
                try:
                    os.remove(row.file_path)
                    removed += 1
                except OSError:
                    pass
            db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not purge old tmp uploads")
    finally:
        db.close()
    return removed


def _acquire_poller_lock() -> bool:
    """Prefer a single poller across processes via Redis; fall back to in-process."""
    client = redis_support.get_redis()
    if client is None:
        return True
    try:
        return bool(client.set("sharesquad:scan-poller", str(os.getpid()), nx=True, ex=30))
    except Exception:
        return True


def _refresh_poller_lock() -> None:
    client = redis_support.get_redis()
    if client is None:
        return
    try:
        client.set("sharesquad:scan-poller", str(os.getpid()), xx=True, ex=30)
    except Exception:
        pass


def start_background_poller(resolve_vlm_config) -> None:
    """Lightweight poller that claims queued jobs when capacity frees up."""
    if not RUN_OCR_POLLER:
        logger.info("OCR poller disabled (RUN_OCR_POLLER=0)")
        return
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    def _loop() -> None:
        owns = False
        while True:
            try:
                if not owns:
                    owns = _acquire_poller_lock()
                if not owns:
                    time.sleep(3)
                    continue
                _refresh_poller_lock()
                reclaim_stale_jobs()
                db = SessionLocal()
                try:
                    jobs = (
                        db.query(models.ScanJob)
                        .filter_by(status="queued")
                        .order_by(models.ScanJob.created_at.asc())
                        .limit(OCR_WORKER_CONCURRENCY)
                        .all()
                    )
                    for job in jobs:
                        vlm_config = resolve_vlm_config(db, job.account_id, job.group_id)
                        kick_scan_worker(job.id, vlm_config)
                finally:
                    db.close()
            except Exception:
                logger.exception("Scan poller error")
                owns = False
            time.sleep(2)

    threading.Thread(target=_loop, name="scan-poller", daemon=True).start()
