"""Use a disposable database and disable background/external work for release tests."""
import os
import tempfile
from pathlib import Path

_test_dir = tempfile.mkdtemp(prefix='test-data-', dir=Path(__file__).resolve().parent)
os.environ.update({
    'DATA_DIR': _test_dir,
    'DATABASE_URL': 'sqlite:///' + _test_dir.replace('\\', '/') + '/tests.db',
    'DISABLE_SCHEDULER': '1',
    'RUN_SCHEDULER': '0',
    'RUN_OCR_POLLER': '0',
    'BILLBUDDY_ENV': 'development',
    'SMTP_HOST': '',
    'REDIS_URL': '',
})
