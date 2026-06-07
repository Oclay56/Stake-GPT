from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


DEFAULT_LOCAL_UI_JOB_TABLE = "local_ui_jobs"
DEFAULT_JOB_RETENTION_HOURS = 6.0
DEFAULT_STALE_RUNNING_MINUTES = 15.0


@dataclass(frozen=True)
class CleanupOperation:
    name: str
    method: str
    filters: dict[str, str]
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class LocalCleanupTarget:
    name: str
    relative_path: str


LOCAL_CLEANUP_TARGETS = [
    LocalCleanupTarget("temporary workspace files", ".tmp"),
    LocalCleanupTarget("pytest cache", ".pytest-cache-local"),
    LocalCleanupTarget("Chrome HTTP cache", "data/chrome-stake-ui/Default/Cache"),
    LocalCleanupTarget("Chrome code cache", "data/chrome-stake-ui/Default/Code Cache"),
    LocalCleanupTarget("Chrome GPU cache", "data/chrome-stake-ui/Default/GPUCache"),
    LocalCleanupTarget("Chrome Dawn graphite cache", "data/chrome-stake-ui/Default/DawnGraphiteCache"),
    LocalCleanupTarget("Chrome Dawn WebGPU cache", "data/chrome-stake-ui/Default/DawnWebGPUCache"),
    LocalCleanupTarget("Chrome service worker cache", "data/chrome-stake-ui/Default/Service Worker/CacheStorage"),
    LocalCleanupTarget("Chrome service worker script cache", "data/chrome-stake-ui/Default/Service Worker/ScriptCache"),
    LocalCleanupTarget("Chrome blob cache", "data/chrome-stake-ui/Default/blob_storage"),
    LocalCleanupTarget("Chrome profile GPU persistent cache", "data/chrome-stake-ui/GPUPersistentCache"),
    LocalCleanupTarget("Chrome profile shader cache", "data/chrome-stake-ui/ShaderCache"),
    LocalCleanupTarget("Chrome profile graph shader cache", "data/chrome-stake-ui/GrShaderCache"),
    LocalCleanupTarget("Chrome browser metrics", "data/chrome-stake-ui/BrowserMetrics"),
    LocalCleanupTarget("Chrome crash reports", "data/chrome-stake-ui/Crashpad/reports"),
    LocalCleanupTarget("Chrome crash attachments", "data/chrome-stake-ui/Crashpad/attachments"),
    LocalCleanupTarget("Chrome component CRX cache", "data/chrome-stake-ui/component_crx_cache"),
    LocalCleanupTarget("Chrome extension CRX cache", "data/chrome-stake-ui/extensions_crx_cache"),
]


def cleanup_operations(
    *,
    now: datetime,
    retention_hours: float = DEFAULT_JOB_RETENTION_HOURS,
    stale_running_minutes: float = DEFAULT_STALE_RUNNING_MINUTES,
) -> list[CleanupOperation]:
    now_text = _utc_iso(now)
    stale_cutoff = _utc_iso(now - timedelta(minutes=max(stale_running_minutes, 1.0)))
    old_cutoff = _utc_iso(now - timedelta(hours=max(retention_hours, 0.25)))
    expired_payload = {
        "status": "expired",
        "error_message": "Expired by AZP Supabase cache cleanup.",
        "completed_at": now_text,
        "updated_at": now_text,
    }

    return [
        CleanupOperation(
            name="expire pending/running jobs past expires_at",
            method="PATCH",
            filters={
                "status": "in.(pending,running)",
                "expires_at": f"lt.{now_text}",
            },
            payload=expired_payload,
        ),
        CleanupOperation(
            name="expire stale running jobs",
            method="PATCH",
            filters={
                "status": "eq.running",
                "updated_at": f"lt.{stale_cutoff}",
            },
            payload=expired_payload,
        ),
        CleanupOperation(
            name="delete jobs past expires_at",
            method="DELETE",
            filters={
                "expires_at": f"lt.{now_text}",
            },
        ),
        CleanupOperation(
            name="delete old completed/failed/expired jobs",
            method="DELETE",
            filters={
                "status": "in.(completed,failed,expired)",
                "updated_at": f"lt.{old_cutoff}",
            },
        ),
    ]


def run_cleanup(
    *,
    supabase_url: str,
    service_key: str,
    table_name: str = DEFAULT_LOCAL_UI_JOB_TABLE,
    retention_hours: float = DEFAULT_JOB_RETENTION_HOURS,
    stale_running_minutes: float = DEFAULT_STALE_RUNNING_MINUTES,
    dry_run: bool = False,
) -> dict[str, Any]:
    table_url = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
    headers = _headers(service_key)
    operations = cleanup_operations(
        now=datetime.now(timezone.utc),
        retention_hours=retention_hours,
        stale_running_minutes=stale_running_minutes,
    )
    results: list[dict[str, Any]] = []

    with httpx.Client(timeout=30) as client:
        for operation in operations:
            matched = _count_rows(client, table_url, headers, operation.filters)
            changed = 0
            if not dry_run and matched:
                _apply_operation(client, table_url, headers, operation)
                changed = matched
            results.append(
                {
                    "operation": operation.name,
                    "method": operation.method,
                    "matched": matched,
                    "changed": changed,
                }
            )

    return {
        "table": table_name,
        "dryRun": dry_run,
        "retentionHours": retention_hours,
        "staleRunningMinutes": stale_running_minutes,
        "expiredJobs": sum(item["changed"] for item in results if item["method"] == "PATCH"),
        "deletedJobs": sum(item["changed"] for item in results if item["method"] == "DELETE"),
        "operations": results,
    }


def cleanup_local_cache(
    *,
    root_dir: Path | str,
    dry_run: bool = False,
    targets: list[LocalCleanupTarget] | None = None,
) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    cleanup_targets = targets or LOCAL_CLEANUP_TARGETS
    results: list[dict[str, Any]] = []
    total_files = 0
    total_dirs = 0
    total_bytes = 0
    errors: list[str] = []

    for target in cleanup_targets:
        path = _safe_local_target_path(root, target.relative_path)
        before = _path_stats(path)
        status = "missing"
        if path.exists():
            status = "would_delete" if dry_run else "deleted"
            if not dry_run:
                try:
                    _delete_local_target(path)
                except OSError as exc:
                    status = "failed"
                    errors.append(f"{target.name}: {exc}")
        after_exists = path.exists()
        changed = bool(before["exists"] and not dry_run and not after_exists and status == "deleted")
        if changed or (dry_run and before["exists"]):
            total_files += int(before["files"])
            total_dirs += int(before["dirs"])
            total_bytes += int(before["bytes"])
        results.append(
            {
                "target": target.name,
                "path": str(path),
                "status": status,
                "files": before["files"],
                "dirs": before["dirs"],
                "bytes": before["bytes"],
            }
        )

    return {
        "rootDir": str(root),
        "dryRun": dry_run,
        "deletedFiles": total_files if not dry_run else 0,
        "deletedDirs": total_dirs if not dry_run else 0,
        "bytesFreed": total_bytes if not dry_run else 0,
        "wouldDeleteFiles": total_files if dry_run else 0,
        "wouldDeleteDirs": total_dirs if dry_run else 0,
        "wouldFreeBytes": total_bytes if dry_run else 0,
        "errors": errors,
        "targets": results,
    }


def _safe_local_target_path(root: Path, relative_path: str) -> Path:
    target = (root / relative_path).resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"Refusing to clean path outside workspace: {target}")
    return target


def _path_stats(path: Path) -> dict[str, int | bool]:
    if not path.exists():
        return {"exists": False, "files": 0, "dirs": 0, "bytes": 0}
    if path.is_file():
        return {"exists": True, "files": 1, "dirs": 0, "bytes": path.stat().st_size}
    files = 0
    dirs = 1
    size = 0
    for child in path.rglob("*"):
        try:
            if child.is_dir():
                dirs += 1
            elif child.is_file():
                files += 1
                size += child.stat().st_size
        except OSError:
            continue
    return {"exists": True, "files": files, "dirs": dirs, "bytes": size}


def _delete_local_target(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _count_rows(
    client: httpx.Client,
    table_url: str,
    headers: dict[str, str],
    filters: dict[str, str],
) -> int:
    response = client.get(
        table_url,
        params={"select": "job_id", **filters},
        headers={
            **headers,
            "Prefer": "count=exact",
            "Range": "0-0",
        },
    )
    _raise_for_supabase_error(response, "count")
    return _content_range_total(response.headers.get("content-range"))


def _apply_operation(
    client: httpx.Client,
    table_url: str,
    headers: dict[str, str],
    operation: CleanupOperation,
) -> None:
    request_headers = {**headers, "Prefer": "return=minimal"}
    if operation.method == "PATCH":
        response = client.patch(
            table_url,
            params=operation.filters,
            headers=request_headers,
            json=operation.payload or {},
        )
    elif operation.method == "DELETE":
        response = client.delete(
            table_url,
            params=operation.filters,
            headers=request_headers,
        )
    else:
        raise ValueError(f"Unsupported cleanup method: {operation.method}")
    _raise_for_supabase_error(response, operation.name)


def _headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def _content_range_total(value: str | None) -> int:
    if not value or "/" not in value:
        return 0
    total = value.rsplit("/", 1)[-1].strip()
    if total == "*":
        return 0
    try:
        return int(total)
    except ValueError:
        return 0


def _raise_for_supabase_error(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    raise RuntimeError(
        f"Supabase cache cleanup failed during {action}: "
        f"{response.status_code} {response.text}"
    )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        size = 0.0
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main() -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Clean old AZP local UI bridge rows from Supabase and rebuildable local cache files.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Count rows without deleting.")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Only clean local rebuildable cache/temp files; skip Supabase.",
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="Only clean Supabase local UI job rows; skip local files.",
    )
    parser.add_argument(
        "--root-dir",
        default=str(Path.cwd()),
        help="Workspace root for local cache cleanup.",
    )
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=_env_float("AZP_SUPABASE_JOB_RETENTION_HOURS", DEFAULT_JOB_RETENTION_HOURS),
        help="Keep completed/failed/expired UI jobs newer than this many hours.",
    )
    parser.add_argument(
        "--stale-running-minutes",
        type=float,
        default=_env_float(
            "AZP_SUPABASE_STALE_JOB_MINUTES",
            DEFAULT_STALE_RUNNING_MINUTES,
        ),
        help="Mark running UI jobs older than this many minutes as expired.",
    )
    parser.add_argument(
        "--table",
        default=os.getenv("AZP_LOCAL_UI_JOB_TABLE", DEFAULT_LOCAL_UI_JOB_TABLE),
        help="Supabase local UI job table name.",
    )
    args = parser.parse_args()
    if args.local_only and args.skip_local:
        print("ERROR: --local-only and --skip-local cannot be used together.")
        return 1

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not args.local_only and (not supabase_url or not service_key):
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required in .env.")
        print("Use --local-only to clean local rebuildable cache files without Supabase credentials.")
        return 1

    result: dict[str, Any] | None = None
    if not args.local_only:
        result = run_cleanup(
            supabase_url=supabase_url or "",
            service_key=service_key or "",
            table_name=args.table,
            retention_hours=args.retention_hours,
            stale_running_minutes=args.stale_running_minutes,
            dry_run=args.dry_run,
        )

    local_result: dict[str, Any] | None = None
    if not args.skip_local:
        local_result = cleanup_local_cache(root_dir=Path(args.root_dir), dry_run=args.dry_run)

    print("AZP cache cleanup")
    print("-----------------")
    print(f"Mode: {'dry run' if args.dry_run else 'cleanup'}")
    print()
    if result is not None:
        print("Supabase local UI job rows")
        print(f"Table: {result['table']}")
        print(f"Retention: {result['retentionHours']} hours")
        print(f"Stale running cutoff: {result['staleRunningMinutes']} minutes")
        for operation in result["operations"]:
            verb = "would change" if result["dryRun"] else "changed"
            print(
                f"- {operation['operation']}: matched {operation['matched']}, "
                f"{verb} {operation['changed']}"
            )
        print(f"Expired jobs: {result['expiredJobs']}")
        print(f"Deleted jobs: {result['deletedJobs']}")
        print("Kept: pending fresh jobs and GPT decision ledger rows.")
        print()

    if local_result is not None:
        print("Local rebuildable cache files")
        print(f"Root: {local_result['rootDir']}")
        bytes_label = "Would free" if local_result["dryRun"] else "Freed"
        files = local_result["wouldDeleteFiles"] if local_result["dryRun"] else local_result["deletedFiles"]
        dirs = local_result["wouldDeleteDirs"] if local_result["dryRun"] else local_result["deletedDirs"]
        bytes_count = local_result["wouldFreeBytes"] if local_result["dryRun"] else local_result["bytesFreed"]
        print(f"{bytes_label}: {_format_bytes(bytes_count)} across {files} files and {dirs} folders")
        for target in local_result["targets"]:
            if target["status"] == "missing":
                continue
            print(
                f"- {target['target']}: {target['status']} "
                f"({_format_bytes(target['bytes'])}, {target['files']} files)"
            )
        if local_result["errors"]:
            print("Errors:")
            for error in local_result["errors"]:
                print(f"- {error}")
        print("Kept: Chrome cookies, local storage, sessions, profile identity, and logs.")

    return 1 if local_result and local_result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
