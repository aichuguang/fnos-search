from __future__ import annotations

from typing import Any, Callable


class ReadinessService:
    """Builds a role-aware readiness response without exposing secrets."""

    def __init__(
        self,
        *,
        process_role: str,
        database_probe: Callable[[], Any],
        docker_socket_mounted: Callable[[], bool],
        remote_worker_status: Callable[[], dict[str, Any]] | None = None,
        local_worker_status: Callable[[], dict[str, Any]] | None = None,
        root_key_probe: Callable[[], Any] | None = None,
        active_provider_accounts_probe: Callable[[], int] | None = None,
        openlist_required: Callable[[], bool] | None = None,
        valid_openlist_mounts_probe: Callable[[], int] | None = None,
        enabled_storage_targets_probe: Callable[[], int] | None = None,
    ) -> None:
        self.process_role = str(process_role or "all")
        self.database_probe = database_probe
        self.docker_socket_mounted = docker_socket_mounted
        self.remote_worker_status = remote_worker_status
        self.local_worker_status = local_worker_status
        self.root_key_probe = root_key_probe
        self.active_provider_accounts_probe = active_provider_accounts_probe
        self.openlist_required = openlist_required
        self.valid_openlist_mounts_probe = valid_openlist_mounts_probe
        self.enabled_storage_targets_probe = enabled_storage_targets_probe

    def status(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        try:
            self.database_probe()
            checks["database"] = {"ok": True, "status": "ok"}
        except Exception:  # noqa: BLE001
            checks["database"] = {"ok": False, "status": "error", "message": "数据库不可用"}

        if self.root_key_probe is not None:
            checks["root_key"] = self._availability_check(
                self.root_key_probe,
                ready_status="configured",
                missing_status="invalid",
                missing_message="FNOS_ROOT_KEY 未配置或无效",
            )

        if self.active_provider_accounts_probe is not None:
            checks["provider_accounts"] = self._count_check(
                self.active_provider_accounts_probe,
                ready_status="ready",
                missing_status="unconfigured",
                missing_message="没有已启用、检测通过且可读取凭证的激活 Provider 账号",
                count_key="active_count",
            )

        if self.openlist_required is not None:
            try:
                requires_openlist = bool(self.openlist_required())
            except Exception:  # noqa: BLE001
                checks["openlist"] = {
                    "ok": False,
                    "required": True,
                    "status": "probe_failed",
                    "message": "无法判断当前业务是否需要 OpenList",
                }
            else:
                if not requires_openlist:
                    checks["openlist"] = {
                        "ok": True,
                        "required": False,
                        "status": "not_required",
                    }
                elif self.valid_openlist_mounts_probe is None:
                    checks["openlist"] = {
                        "ok": False,
                        "required": True,
                        "status": "unconfigured",
                        "message": "Organizer 或跨盘链路已启用，但 OpenList 就绪检查未配置",
                    }
                else:
                    checks["openlist"] = self._count_check(
                        self.valid_openlist_mounts_probe,
                        ready_status="ready",
                        missing_status="unconfigured",
                        missing_message=(
                            "Organizer 或跨盘链路已启用，但没有检测通过的数据库 OpenList 连接与挂载"
                        ),
                        count_key="valid_mount_count",
                    )

        if self.enabled_storage_targets_probe is not None:
            storage_targets = self._count_check(
                self.enabled_storage_targets_probe,
                ready_status="configured",
                missing_status="unconfigured",
                missing_message="尚未配置启用的存储目标，进程可就绪但入库业务不可用",
                count_key="enabled_count",
            )
            storage_targets["required"] = False
            # 存储目标属于业务配置，不是进程依赖。缺少目标时保留明确状态，
            # 但绝不能把 Web/Worker 的 readiness 整体拉低。
            storage_targets["configured"] = bool(storage_targets.get("ok"))
            storage_targets["ok"] = True
            checks["storage_targets"] = storage_targets

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

        ok = all(
            bool(item.get("ok"))
            for item in checks.values()
            if bool(item.get("required", True))
        )
        failed = [
            name
            for name, item in checks.items()
            if bool(item.get("required", True)) and not item.get("ok")
        ]
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

    @staticmethod
    def _availability_check(
        callback: Callable[[], Any],
        *,
        ready_status: str,
        missing_status: str,
        missing_message: str,
    ) -> dict[str, Any]:
        try:
            available = bool(callback())
        except Exception:  # noqa: BLE001 - readiness 永不回显底层异常或密钥内容
            available = False
        return {
            "ok": available,
            "status": ready_status if available else missing_status,
            **({"message": missing_message} if not available else {}),
        }

    @staticmethod
    def _count_check(
        callback: Callable[[], int],
        *,
        ready_status: str,
        missing_status: str,
        missing_message: str,
        count_key: str,
    ) -> dict[str, Any]:
        try:
            count = max(0, int(callback()))
            failed = False
        except Exception:  # noqa: BLE001 - 只输出稳定、无敏感信息的状态
            count = 0
            failed = True
        ok = count > 0 and not failed
        return {
            "ok": ok,
            "status": ready_status if ok else ("probe_failed" if failed else missing_status),
            count_key: count,
            **({"message": missing_message} if not ok else {}),
        }
