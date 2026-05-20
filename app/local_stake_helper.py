from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .local_ui_bridge import STAKE_SGM_JOB_TYPE, SupabaseLocalUiJobStore
from .stake_sgm_browser import DEFAULT_CDP_URL, read_stake_sgm_board


DEFAULT_CHROME_USER_DATA_DIR = Path("data") / "chrome-stake-ui"


async def run_helper(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    poll_seconds: float = 2.0,
    worker_id: str | None = None,
    autostart_chrome: bool = True,
) -> None:
    _load_dotenv()
    store = SupabaseLocalUiJobStore()
    if not store.enabled():
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required before starting "
            "the local AZP helper."
        )

    if autostart_chrome:
        ensure_debug_chrome(cdp_url)

    resolved_worker_id = worker_id or f"azp-local-{socket.gethostname()}"
    print("AZP Local Helper")
    print(f"Status: waiting for Stake UI jobs as {resolved_worker_id}")
    print("Stop: press Ctrl+C in this window.")

    while True:
        job = await store.claim_next_pending_job(
            worker_id=resolved_worker_id,
            job_type=STAKE_SGM_JOB_TYPE,
        )
        if not job:
            await asyncio.sleep(max(poll_seconds, 0.5))
            continue

        await process_job(store, job, cdp_url=cdp_url)


async def process_job(
    store: SupabaseLocalUiJobStore,
    job: dict[str, Any],
    *,
    cdp_url: str = DEFAULT_CDP_URL,
) -> None:
    job_id = str(job["jobId"])
    request = job.get("request") or {}
    fixture_slug = str(request.get("fixtureSlug") or "").strip()
    if not fixture_slug:
        await store.fail_job(job_id, "Job request is missing fixtureSlug.")
        return

    print(f"[{time.strftime('%H:%M:%S')}] Reading Stake SGM: {fixture_slug}")
    try:
        board = await asyncio.to_thread(
            read_stake_sgm_board,
            fixture_slug,
            cdp_url=cdp_url,
        )
        board["request"] = request
        await store.complete_job(job_id, board)
        print(f"[{time.strftime('%H:%M:%S')}] Completed job {job_id}")
    except Exception as exc:
        await store.fail_job(job_id, str(exc))
        print(f"[{time.strftime('%H:%M:%S')}] Failed job {job_id}: {exc}")


def ensure_debug_chrome(cdp_url: str = DEFAULT_CDP_URL) -> None:
    if _cdp_is_ready(cdp_url):
        return

    chrome_path = _chrome_path()
    if not chrome_path:
        raise RuntimeError(
            "Could not find Chrome. Set AZP_CHROME_PATH to chrome.exe and retry."
        )

    profile_dir = Path(os.getenv("AZP_STAKE_CHROME_PROFILE") or DEFAULT_CHROME_USER_DATA_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _cdp_port(cdp_url)
    subprocess.Popen(
        [
            str(chrome_path),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir.resolve()}",
            "--window-size=1200,900",
            "--window-position=-32000,-32000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _cdp_is_ready(cdp_url):
            return
        time.sleep(0.5)
    raise RuntimeError("Chrome did not expose the remote debugging port in time.")


def _cdp_is_ready(cdp_url: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _cdp_port(cdp_url: str) -> int:
    return int(cdp_url.rstrip("/").rsplit(":", 1)[-1])


def _chrome_path() -> Path | None:
    configured = os.getenv("AZP_CHROME_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LocalAppData", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or Path.cwd() / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Run the AZP local Stake UI helper.")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id")
    parser.add_argument("--no-autostart-chrome", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(
            run_helper(
                cdp_url=args.cdp_url,
                poll_seconds=args.poll_seconds,
                worker_id=args.worker_id,
                autostart_chrome=not args.no_autostart_chrome,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("AZP Local Helper stopped.")
        return 0
    except Exception as exc:
        print(f"AZP Local Helper error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
