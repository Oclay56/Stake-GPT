from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


STAKE_SGM_BOARD_JOB_TYPE = "stake_ui_sgm_board"
STAKE_SGM_BUILD_SLIP_JOB_TYPE = "stake_ui_sgm_build_slip"

# Backward-compatible name used by the first local helper implementation.
STAKE_SGM_JOB_TYPE = STAKE_SGM_BOARD_JOB_TYPE


class LocalUiBridgeError(RuntimeError):
    pass


class LocalUiBridgeDisabled(LocalUiBridgeError):
    pass


class LocalUiBridgeTimeout(LocalUiBridgeError):
    pass


class SupabaseLocalUiJobStore:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_key: str | None = None,
        table_name: str | None = None,
    ) -> None:
        self.supabase_url = supabase_url or os.getenv("SUPABASE_URL")
        self.service_key = (
            service_key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_SERVICE_KEY")
        )
        self.table_name = table_name or os.getenv(
            "AZP_LOCAL_UI_JOB_TABLE",
            "local_ui_jobs",
        )

    def enabled(self) -> bool:
        return bool(self.supabase_url and self.service_key)

    async def create_job(
        self,
        *,
        job_type: str,
        request: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self._require_enabled()
        job_id = str(uuid.uuid4())
        now = _utc_now()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(timeout_seconds, 1) + 60)
        ).isoformat()
        row = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "pending",
            "request_json": request,
            "result_json": None,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
        }
        rows = await self._request(
            "POST",
            self._table_url(),
            params={"select": "*"},
            headers={"Prefer": "return=representation"},
            json=[row],
        )
        return _row_to_job(rows[0])

    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 1)
        while True:
            job = await self.get_job(job_id)
            if job.get("status") in {"completed", "failed", "expired"}:
                return job
            if asyncio.get_running_loop().time() >= deadline:
                raise LocalUiBridgeTimeout(
                    "Timed out waiting for the local AZP helper to return Stake UI data."
                )
            await asyncio.sleep(max(poll_interval_seconds, 0.25))

    async def get_job(self, job_id: str) -> dict[str, Any]:
        self._require_enabled()
        rows = await self._request(
            "GET",
            self._table_url(),
            params={
                "select": "*",
                "job_id": f"eq.{job_id}",
                "limit": "1",
            },
        )
        if not rows:
            raise LocalUiBridgeError(f"Local UI job was not found: {job_id}")
        return _row_to_job(rows[0])

    async def find_recent_completed_job(
        self,
        *,
        job_type: str,
        fixture_slug: str,
        max_age_seconds: int,
        limit: int = 20,
    ) -> dict[str, Any] | None:
        self._require_enabled()
        if max_age_seconds <= 0 or not fixture_slug:
            return None

        rows = await self._request(
            "GET",
            self._table_url(),
            params={
                "select": "*",
                "job_type": f"eq.{job_type}",
                "status": "eq.completed",
                "order": "updated_at.desc",
                "limit": str(max(limit, 1)),
            },
        )
        now = datetime.now(timezone.utc)
        for row in rows or []:
            request = row.get("request_json") or {}
            if str(request.get("fixtureSlug") or "") != fixture_slug:
                continue

            updated_at = _parse_utc_datetime(
                row.get("completed_at") or row.get("updated_at") or row.get("created_at")
            )
            if not updated_at:
                continue
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds <= max_age_seconds:
                return _row_to_job(row)
        return None

    async def claim_next_pending_job(
        self,
        *,
        worker_id: str,
        job_type: str = STAKE_SGM_JOB_TYPE,
    ) -> dict[str, Any] | None:
        self._require_enabled()
        rows = await self._request(
            "GET",
            self._table_url(),
            params={
                "select": "*",
                "job_type": f"eq.{job_type}",
                "status": "eq.pending",
                "order": "created_at.asc",
                "limit": "1",
            },
        )
        if not rows:
            return None

        job_id = rows[0]["job_id"]
        now = _utc_now()
        claimed = await self._request(
            "PATCH",
            self._table_url(),
            params={
                "select": "*",
                "job_id": f"eq.{job_id}",
                "status": "eq.pending",
            },
            headers={"Prefer": "return=representation"},
            json={
                "status": "running",
                "worker_id": worker_id,
                "claimed_at": now,
                "updated_at": now,
            },
        )
        return _row_to_job(claimed[0]) if claimed else None

    async def complete_job(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return await self._finish_job(
            job_id,
            status="completed",
            result=result,
            error_message=None,
        )

    async def fail_job(self, job_id: str, error_message: str) -> dict[str, Any]:
        return await self._finish_job(
            job_id,
            status="failed",
            result=None,
            error_message=error_message,
        )

    async def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error_message: str | None,
    ) -> dict[str, Any]:
        self._require_enabled()
        now = _utc_now()
        rows = await self._request(
            "PATCH",
            self._table_url(),
            params={"select": "*", "job_id": f"eq.{job_id}"},
            headers={"Prefer": "return=representation"},
            json={
                "status": status,
                "result_json": result,
                "error_message": error_message,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if not rows:
            raise LocalUiBridgeError(f"Local UI job was not found: {job_id}")
        return _row_to_job(rows[0])

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        request_headers = self._headers()
        if headers:
            request_headers.update(headers)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method,
                url,
                params=params,
                headers=request_headers,
                json=json,
            )
        if response.status_code >= 400:
            raise LocalUiBridgeError(
                f"Supabase local UI job request failed: "
                f"{response.status_code} {response.text}"
            )
        if not response.content:
            return None
        return response.json()

    def _headers(self) -> dict[str, str]:
        self._require_enabled()
        return {
            "apikey": str(self.service_key),
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }

    def _table_url(self) -> str:
        self._require_enabled()
        return f"{str(self.supabase_url).rstrip('/')}/rest/v1/{self.table_name}"

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise LocalUiBridgeDisabled(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the "
                "local UI bridge."
            )


def _row_to_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobId": row.get("job_id"),
        "jobType": row.get("job_type"),
        "status": row.get("status"),
        "request": row.get("request_json") or {},
        "result": row.get("result_json"),
        "error": row.get("error_message"),
        "workerId": row.get("worker_id"),
        "createdAt": row.get("created_at"),
        "claimedAt": row.get("claimed_at"),
        "completedAt": row.get("completed_at"),
        "updatedAt": row.get("updated_at"),
        "expiresAt": row.get("expires_at"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
