"""Dedicated OCR worker process entrypoint (docker compose scan-worker)."""
from __future__ import annotations

import logging
import time

from .scan_worker import start_background_poller
from .settings_service import resolve_vlm_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("scan_worker_main")


def _resolve(db, account_id: int, group_id):
    return resolve_vlm_config(db, account_id, group_id)


def main() -> None:
    logger.info("Starting dedicated scan worker poller")
    start_background_poller(_resolve)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
