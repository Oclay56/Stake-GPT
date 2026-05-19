from __future__ import annotations

import argparse
import asyncio
import os
import socket
import time
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .stake_ui_builder import StakeUiBuildConfig, build_stake_ui_slip


DEFAULT_RENDER_API_URL = "https://azp-gpt-action.onrender.com"
DEFAULT_STAKE_URL = "https://stake.com/sports/baseball"
UiRunner = Callable[[dict[str, Any], StakeUiBuildConfig], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class LocalBridgeConfig:
    api_url: str = DEFAULT_RENDER_API_URL
    api_key: str | None = None
    bridge_id: str = "azp-local-bridge"
    poll_seconds: float = 10.0
    stake_url: str = DEFAULT_STAKE_URL
    open_browser: bool = False
    ui_mode: str = "dry_run"

    @classmethod
    def from_env(cls) -> "LocalBridgeConfig":
        load_env_file()
        return cls(
            api_url=(
                os.getenv("AZP_BRIDGE_API_URL")
                or os.getenv("AZP_API_URL")
                or DEFAULT_RENDER_API_URL
            ).rstrip("/"),
            api_key=os.getenv("AZP_BRIDGE_API_KEY") or os.getenv("AZP_GPT_API_KEY") or None,
            bridge_id=os.getenv("AZP_BRIDGE_ID") or f"azp-{socket.gethostname()}",
            poll_seconds=_float_env("AZP_BRIDGE_POLL_SECONDS", 10.0),
            stake_url=os.getenv("AZP_BRIDGE_STAKE_URL") or DEFAULT_STAKE_URL,
            open_browser=_truthy(os.getenv("AZP_BRIDGE_OPEN_BROWSER")),
            ui_mode=_clean_ui_mode(os.getenv("AZP_BRIDGE_UI_MODE") or "dry_run"),
        )


class SlipJobApiClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        config: LocalBridgeConfig,
    ) -> None:
        self._http_client = http_client
        self._config = config

    async def claim_next_job(self) -> dict[str, Any] | None:
        response = await self._http_client.get(
            "/slip-jobs/next",
            params={"bridgeId": self._config.bridge_id},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        job = payload.get("job")
        return job if isinstance(job, dict) else None

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        message: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._http_client.post(
            f"/slip-jobs/{job_id}/status",
            headers=self._headers(),
            json={
                "status": status,
                "bridgeId": self._config.bridge_id,
                "message": message,
                "result": result or {},
            },
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        if not self._config.api_key:
            return {}
        return {"X-AZP-API-Key": self._config.api_key}


async def run_once(
    config: LocalBridgeConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
    ui_runner: UiRunner | None = None,
) -> dict[str, Any]:
    if http_client is not None:
        return await _run_once_with_client(config, http_client, ui_runner=ui_runner)

    async with httpx.AsyncClient(base_url=config.api_url, timeout=30) as owned_client:
        return await _run_once_with_client(config, owned_client, ui_runner=ui_runner)


async def _run_once_with_client(
    config: LocalBridgeConfig,
    http_client: httpx.AsyncClient,
    *,
    ui_runner: UiRunner | None = None,
) -> dict[str, Any]:
    api_client = SlipJobApiClient(http_client=http_client, config=config)
    job = await api_client.claim_next_job()
    if not job:
        return {"status": "idle", "message": "No pending slip jobs."}

    print(format_job_summary(job))
    if config.open_browser and config.ui_mode == "dry_run":
        webbrowser.open(config.stake_url)

    result = await _build_ui_result(job, config, ui_runner=ui_runner)
    next_status = _status_from_ui_result(result)
    updated = await api_client.update_job_status(
        job["jobId"],
        next_status,
        message=result.get("message"),
        result=result,
    )
    return {"status": "processed", "job": updated}


async def watch(config: LocalBridgeConfig) -> None:
    print(f"AZP Local Bridge watching {config.api_url}")
    print(f"Bridge ID: {config.bridge_id}")
    print(f"UI mode: {config.ui_mode}")
    print("Close this window to stop the bridge.")
    was_waiting = False
    while True:
        try:
            outcome = await run_once(config)
            message, was_waiting = watch_message(outcome, was_waiting=was_waiting)
            if message:
                print(f"[{_clock()}] {message}")
        except Exception as exc:
            was_waiting = False
            print(f"[{_clock()}] Bridge error: {exc}")
        await asyncio.sleep(max(2.0, config.poll_seconds))


def build_dry_run_result(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "matched": 0,
        "blocked": 0,
        "requiresManualReview": True,
        "uiAutomationEnabled": False,
        "message": (
            "This pass confirms the handoff and opens Stake when configured. "
            "Exact UI selectors still need live calibration before click mode."
        ),
        "legs": [
            {
                "index": index,
                "player": ((selection.get("player") or {}).get("name")),
                "market": ((selection.get("market") or {}).get("name") or (selection.get("market") or {}).get("key")),
                "side": selection.get("side"),
                "line": selection.get("line"),
                "odds": selection.get("odds"),
                "action": "review_only_not_clicked",
            }
            for index, selection in enumerate(job.get("selections") or [], start=1)
        ],
    }


def format_job_summary(job: dict[str, Any]) -> str:
    lines = [
        "",
        "AZP Slip Job",
        f"Job: {job.get('jobId')}",
        f"Matchup: {job.get('matchup') or 'mixed/slate'}",
        f"Date: {job.get('date') or 'not specified'}",
        f"Legs: {len(job.get('selections') or [])}",
    ]
    for index, selection in enumerate(job.get("selections") or [], start=1):
        player = (selection.get("player") or {}).get("name") or "Unknown player"
        market = (
            (selection.get("market") or {}).get("name")
            or (selection.get("market") or {}).get("key")
            or "market"
        )
        side = selection.get("side") or "side"
        line = selection.get("line")
        odds = selection.get("odds")
        lines.append(f"{index}. {player} {side} {line} {market} @ {odds}")
    return "\n".join(lines)


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AZP local slip bridge")
    parser.add_argument("command", choices=["once", "watch"], help="Run one poll or watch forever.")
    parser.add_argument("--api-url", help="AZP API base URL.")
    parser.add_argument("--api-key", help="AZP API key header value.")
    parser.add_argument("--bridge-id", help="Stable local bridge identifier.")
    parser.add_argument("--poll-seconds", type=float, help="Polling interval for watch mode.")
    parser.add_argument("--open-browser", action="store_true", help="Open Stake when a job is claimed.")
    parser.add_argument(
        "--ui-mode",
        choices=["dry_run", "dry-run", "audit", "click"],
        help="dry_run prints only, audit verifies visible UI, click selects exact matched legs.",
    )
    args = parser.parse_args(argv)

    base = LocalBridgeConfig.from_env()
    config = LocalBridgeConfig(
        api_url=(args.api_url or base.api_url).rstrip("/"),
        api_key=args.api_key or base.api_key,
        bridge_id=args.bridge_id or base.bridge_id,
        poll_seconds=args.poll_seconds or base.poll_seconds,
        stake_url=base.stake_url,
        open_browser=args.open_browser or base.open_browser,
        ui_mode=_clean_ui_mode(args.ui_mode or base.ui_mode),
    )

    if args.command == "once":
        outcome = asyncio.run(run_once(config))
        print(outcome.get("message") or outcome.get("status"))
        return 0

    asyncio.run(watch(config))
    return 0


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def watch_message(
    outcome: dict[str, Any],
    *,
    was_waiting: bool,
) -> tuple[str | None, bool]:
    if outcome.get("status") == "idle":
        if was_waiting:
            return None, True
        return "Waiting for slip...", True

    job_status = ((outcome.get("job") or {}).get("status") or "").strip().lower()
    if job_status == "built":
        return "One slip created. Waiting for next slip...", False
    if job_status == "blocked":
        return "Slip blocked. Waiting for next slip...", False
    if job_status == "failed":
        return "Slip failed. Waiting for next slip...", False
    return "Slip job processed. Waiting for next slip...", False


async def _build_ui_result(
    job: dict[str, Any],
    config: LocalBridgeConfig,
    *,
    ui_runner: UiRunner | None = None,
) -> dict[str, Any]:
    ui_config = replace(
        StakeUiBuildConfig.from_env(),
        stake_url=config.stake_url,
        mode=config.ui_mode,
    )
    runner = ui_runner or build_stake_ui_slip
    return await runner(job, ui_config)


def _status_from_ui_result(result: dict[str, Any]) -> str:
    mode = result.get("mode")
    if result.get("blocked", 0):
        return "blocked"
    if mode == "click" and result.get("clicked", 0):
        return "built"
    if mode == "audit":
        return "ui_audit_ready"
    return "dry_run_ready"


def _clean_ui_mode(value: str | None) -> str:
    mode = str(value or "dry_run").strip().lower().replace("-", "_")
    return mode if mode in {"dry_run", "audit", "click"} else "dry_run"


if __name__ == "__main__":
    raise SystemExit(main())
