from __future__ import annotations

from typing import Any, Callable


class ReadinessService:
    """Builds a role-aware readiness response without exposing secrets."""

    def __init__(
        self,
        *,
        process_role: str,
        database_probe: Callable[[], Any],
        deployment_degraded: Callable[[], bool],
        docker_socket_mounted: Callable[[], bool],
        remote_worker_status: Callable[[], dict[str, Any]] | None = None,
        local_worker_status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.process_role = str(process_role or "all")
        self.database_probe = database_probe
        self.deployment_degraded = deployment_degraded
        self.docker_socket_mounted = docker_socket_mounted
        self.remote_worker_status = remote_worker_status
        self.local_worker_status = local_worker_status

    def status(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        try:
            self.database_probe()
            checks["database"] = {"ok": True, "status": "ok"}
        except Exception:  # noqa: BLE001
            checks["database"] = {"ok": False, "status": "error", "message": "数据库不可用"}

        degraded = bool(self.deployment_degraded())
        checks["deployment_layout"] = {
            "ok": not degraded,
            "status": "ok" if not degraded else "legacy_compose",
            **(
                {}
                if not degraded
                else {"message": "正式环境缺少 FNOS_PROCESS_ROLE，请替换为当前版本编排文件"}
            ),
        }
        socket_mounted = bool(self.docker_socket_mounted())
        checks["docker_socket"] = {
            "ok": not socket_mounted,
            "status": "absent" if not socket_mounted else "mounted",
            **(
                {}
                if not socket_mounted
                else {"message": "检测到 Docker Socket 挂载，请替换为无 Socket 的当前编排"}
            ),
        }

        if self.process_role == "web":
            if degraded:
                worker = {
                    "success": False,
                    "healthy": False,
                    "status": "upgrade_required",
                    "message": "旧编排已安全降级，rclone Worker 不会在 Web 容器中启动",
                }
            else:
                worker = self._worker_status(self.remote_worker_status)
            checks["worker"] = {
                "ok": bool(worker.get("success") and worker.get("healthy")),
                "status": str(worker.get("status") or ("ok" if worker.get("healthy") else "worker_unavailable")),
                **({"message": str(worker.get("message"))} if worker.get("message") else {}),
            }
        elif self.local_worker_status is not None:
            worker = self._worker_status(self.local_worker_status)
            checks["worker_runtime"] = {
                "ok": bool(worker.get("success") and worker.get("healthy")),
                "status": str(worker.get("status") or ("ok" if worker.get("healthy") else "unhealthy")),
                **({"message": str(worker.get("message"))} if worker.get("message") else {}),
            }

        ok = all(bool(item.get("ok")) for item in checks.values())
        failed = [name for name, item in checks.items() if not item.get("ok")]
        return {
            "ok": ok,
            "status": "ready" if ok else "not_ready",
            "role": self.process_role,
            "database": "ok" if checks["database"]["ok"] else "error",
            "checks": checks,
            **({"message": "未就绪：" + "、".join(failed)} if failed else {}),
        }

    @staticmethod
    def _worker_status(callback: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        if callback is None:
            return {
                "success": False,
                "healthy": False,
                "status": "worker_unavailable",
                "message": "Worker 状态检查未配置",
            }
        try:
            value = callback()
        except Exception:  # noqa: BLE001
            return {
                "success": False,
                "healthy": False,
                "status": "worker_unavailable",
                "message": "无法获取 Worker 状态",
            }
        if not isinstance(value, dict):
            return {
                "success": False,
                "healthy": False,
                "status": "worker_unavailable",
                "message": "Worker 状态格式不正确",
            }
        return value
