"""
Development server: watches app/ only and restarts on Python changes.
Runs on port 8001 so the original Expense Splitter can stay on 8000.
"""
import os
import subprocess
import sys
from pathlib import Path

from watchfiles import PythonFilter, watch

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
HOST = "0.0.0.0"
PORT = 8001


def _ensure_fontconfig_env(env: dict) -> None:
    """Avoid WeasyPrint Fontconfig spam when Tesseract's DLL is on PATH."""
    conf = APP_DIR / "fontconfig" / "fonts.conf"
    if not conf.is_file():
        return
    env.setdefault("FONTCONFIG_FILE", str(conf))
    env.setdefault("FONTCONFIG_PATH", str(conf.parent))


def start_server() -> subprocess.Popen:
    env = os.environ.copy()
    env["BILLBUDDY_ENV"] = "development"
    env["SHARESQUAD_ENV"] = "development"
    _ensure_fontconfig_env(env)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", f"--host={HOST}", f"--port={PORT}"],
        cwd=ROOT,
        env=env,
    )


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> None:
    print(f"ShareSquad running at http://localhost:{PORT} (LAN: http://0.0.0.0:{PORT})")
    print(f"Watching {APP_DIR} for Python changes. Press Ctrl+C to stop.")
    proc = start_server()
    try:
        # debounce batches rapid multi-file saves into one restart
        for _changes in watch(
            APP_DIR,
            watch_filter=PythonFilter(),
            raise_interrupt=False,
            debounce=1500,
        ):
            print("app/ changed — restarting server...")
            stop_server(proc)
            proc = start_server()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(proc)


if __name__ == "__main__":
    main()
