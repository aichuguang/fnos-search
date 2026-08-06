from __future__ import annotations

import time
from typing import Any, Callable

import requests


class RcloneWorkerRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.payload = dict(payload or {})


class RcloneWorkerClient:
    """Synchronous control-plane client for the isolated rclone worker."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        database: Any,
        timeout_seconds: int = 30,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = str(base_url or "http://fnos-rclone-worker:5251").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.database = database
        self.timeout_seconds = max(3, min(int(timeout_seconds or 30), 300))
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.sleep = sleep

    def status(self) -> dict[str, Any]:
        response = self._request("GET", "/api/internal/rclone/status", retry_safe=True)
        if response.get("success") and isinstance(response.get("status"), dict):
            return response["status"]
        failure_status = str(response.get("status") or "worker_unavailable")
        return {
            "enabled": True,
            "running": False,
            "status": failure_status,
            "error_code": str(response.get("error_code") or failure_status),
            "queue_count": 0,
            "current_run_id": None,
            "last_error": response.get("message") or "rclone Worker 不可用",
            "worker_available": False,
            "_http_status": int(response.get("_http_status") or 503),
        }

    def system_status(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/internal/worker/status",
            retry_safe=True,
            timeout_seconds=3,
        )
        if response.get("success"):
            return response
        return {
            **response,
            "success": False,
            "healthy": False,
            "status": str(response.get("status") or "worker_unavailable"),
        }

    def start(
        self,
        reason: str = "manual",
        file_retry: dict[str, Any] | None = None,
        category_filter: str = "",
        staging_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/start",
            {
                "reason": str(reason or "manual"),
                "file_retry": dict(file_retry) if isinstance(file_retry, dict) else None,
                "category_filter": str(category_filter or ""),
                "staging_run": dict(staging_run) if isinstance(staging_run, dict) else None,
            },
        )

    def stop(self) -> dict[str, Any]:
        return self._request("POST", "/api/internal/rclone/stop", {})

    def check_environment(self) -> dict[str, Any]:
        return self._request("POST", "/api/internal/rclone/check", {}, timeout_seconds=120)

    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/cancel-job",
            {"job_id": int(job_id), "stop_running": bool(stop_running)},
        )

    def cleanup_cancelled_task(self, **kwargs: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/cleanup-cancelled-task",
            kwargs,
            timeout_seconds=120,
        )

    def start_file_retry(self, file_event: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/file-retry",
            {"file_event": dict(file_event)},
        )

    def webdav_status(self, remote_name: Any = None) -> dict[str, Any]:
        params = {"remote_name": str(remote_name)} if remote_name else None
        return self._request(
            "GET",
            "/api/internal/rclone/webdav-config",
            params=params,
            retry_safe=True,
            raise_for_error=True,
        )

    def webdav_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/webdav-config",
            dict(payload),
            timeout_seconds=120,
            raise_for_error=True,
        )

    def webdav_test(self, remote_name: Any = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/rclone/webdav-config/test",
            {"remote_name": remote_name},
            timeout_seconds=120,
            raise_for_error=True,
        )

    def reload(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/internal/runtime/reload",
            {},
            timeout_seconds=120,
            retry_safe=True,
        )

    def get_logs(self, *, limit: int) -> list[Any] | dict[str, Any]:
        response = self._request(
            "GET",
            "/api/internal/rclone/logs",
            params={"limit": max(1, int(limit or 200))},
            retry_safe=True,
        )
        if response.get("success") and isinstance(response.get("items"), list):
            return response["items"]
        return response

    def list_runs(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.database.list_rclone_runs(limit=limit, offset=offset)

    def list_events(self, *, run_id: int | None, limit: int) -> list[dict[str, Any]]:
        return self.database.list_rclone_events(run_id=run_id, limit=limit)

    def list_file_events(self, **filters: Any) -> list[dict[str, Any]]:
        return self.database.list_rclone_file_events(**filters)

    def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        retry_safe: bool = False,
        raise_for_error: bool = False,
    ) -> dict[str, Any]:
        if not self.token:
            return self._failed_response(
                "rclone Worker 控制令牌未初始化",
                503,
                raise_for_error=raise_for_error,
            )
        response: Any = None
        attempts = 3 if retry_safe else 1
        delays = (0.2, 0.5)
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload if method.upper() != "GET" else None,
                    params=params,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=timeout_seconds or self.timeout_seconds,
                )
            except requests.Timeout:
                if attempt + 1 < attempts:
                    self.sleep(delays[min(attempt, len(delays) - 1)])
                    continue
                return self._failed_response(
                    "连接 rclone Worker 超时",
                    504,
                    raise_for_error=raise_for_error,
                )
            except requests.RequestException:
                if attempt + 1 < attempts:
                    self.sleep(delays[min(attempt, len(delays) - 1)])
                    continue
                return self._failed_response(
                    "无法连接 rclone Worker，请稍后重试",
                    503,
                    raise_for_error=raise_for_error,
                )
            if response.status_code in {502, 503, 504} and attempt + 1 < attempts:
                self.sleep(delays[min(attempt, len(delays) - 1)])
                continue
            break
        if response is None:
            return self._failed_response("rclone Worker 不可用", 503, raise_for_error=raise_for_error)
        try:
            data = response.json()
        except ValueError:
            data = {"success": False, "message": "rclone Worker 返回了无法解析的响应"}
        if not isinstance(data, dict):
            data = {"success": False, "message": "rclone Worker 返回格式不正确"}
        if response.status_code >= 400:
            data["success"] = False
            data.setdefault("message", f"rclone Worker 请求失败：HTTP {response.status_code}")
            data.setdefault("status", "worker_unavailable" if response.status_code >= 500 else "worker_rejected")
            data.setdefault("error_code", data["status"])
            data["_http_status"] = int(response.status_code)
            if raise_for_error:
                raise RcloneWorkerRequestError(str(data["message"]), response.status_code, data)
        return data

    @staticmethod
    def _failed_response(
        message: str,
        status_code: int,
        *,
        raise_for_error: bool,
    ) -> dict[str, Any]:
        status = "worker_timeout" if status_code == 504 else "worker_unavailable"
        payload = {
            "success": False,
            "status": status,
            "error_code": status,
            "message": message,
            "_http_status": int(status_code),
        }
        if raise_for_error:
            raise RcloneWorkerRequestError(message, status_code, payload)
        return payload
