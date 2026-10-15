from __future__ import annotations

import json
import hmac
import hashlib
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any
from urllib.parse import urlsplit

from ..openlist.runtime_sync import OpenListSyncConflict, managed_remote_identity


RCLONE_UNAVAILABLE = "RCLONE_UNAVAILABLE"
_AUTO_CONFIRMATION_MESSAGES = {
    "unbound": "OpenList 连接服务尚未绑定，无法自动核验托管 Remote。",
    "connection": "自动核验要求唯一且已启用的 OpenList 连接，未修改托管 Remote。",
    "credentials": "OpenList 当前连接修订或已保存凭据不可用，未修改托管 Remote。",
    "marker": "托管 Remote 确认记录损坏或格式未知，已停止自动恢复，请保留原配置排查。",
    "scope": "托管 Remote 名称或配置文件路径已变化，不能自动复用或覆盖原确认。",
    "remote": "实际 rclone Remote 的类型、地址或账号与当前 OpenList 连接不一致，已拒绝自动覆盖。",
    "confirmed": "托管 rclone Remote 身份已确认，无需重新保存 OpenList。",
    "restored": "已使用原有 OpenList 连接自动同步、验证并恢复托管 Remote 确认。",
    "unknown": "托管 Remote 自动核验未完成，未确认可用，请稍后重试或检查 Worker 状态。",
    "waiting": "托管 Remote 尚未完成确认，Worker 会在搬运前自动核验并恢复；若仍失败，请检查具体连接、目录权限和网络原因。",
    "changed": "检测期间托管 Remote 身份已变化，已停止确认；请检查并恢复原连接配置后重试。",
    "timeout": "rclone 操作超时，写入结果尚未确认；Worker 会在下次搬运前核验实际 Remote，当前未确认可用。",
}


class RcloneWebdavConfigError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400, *, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        # ``RCLONE_UNAVAILABLE`` marks "the binary/runtime is not here at all",
        # as opposed to "rclone ran and rejected the request".  Callers that
        # only need rclone for cross-provider transfers degrade on the former
        # instead of failing an operation that never required rclone.
        self.code = str(code or "")


class RcloneWebdavConfigService:
    """Manage one OpenList WebDAV remote with the worker-local rclone binary."""

    CONFIG_PATH = "/config/rclone/rclone.conf"
    BACKUP_PATH = "/config/rclone/rclone.conf.webui.bak"
    _CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
    _BACKUP_SCRIPT = """set -eu
IFS= read -r config
IFS= read -r backup
temporary="${backup}.tmp.$$"
[ -f "$config" ] || : > "$config"
rm -f "$temporary"
cp "$config" "$temporary"
chmod 600 "$temporary"
exec mv -f "$temporary" "$backup"
"""
    _RESTORE_SCRIPT = """set -eu
IFS= read -r config
IFS= read -r backup
temporary="${config}.webui.restore.tmp.$$"
[ -f "$backup" ]
rm -f "$temporary"
cp "$backup" "$temporary"
chmod 660 "$temporary"
exec mv -f "$temporary" "$config"
"""
    _SAVE_SCRIPT = """set -eu
IFS= read -r config
IFS= read -r operation
IFS= read -r remote_name
IFS= read -r webdav_url
IFS= read -r webdav_user
IFS= read -r webdav_password
if [ "$operation" = "create" ]; then
  exec rclone config create "$remote_name" webdav url "$webdav_url" vendor other user "$webdav_user" pass "$webdav_password" --obscure --config "$config"
elif [ -n "$webdav_password" ]; then
  exec rclone config update "$remote_name" url "$webdav_url" vendor other user "$webdav_user" pass "$webdav_password" --obscure --config "$config"
else
  exec rclone config update "$remote_name" url "$webdav_url" vendor other user "$webdav_user" --config "$config"
fi
"""

    def __init__(
        self,
        current_config: Callable[[], Mapping[str, Any]],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 30,
    ) -> None:
        self._current_config = current_config
        self._runner = runner
        self._timeout_seconds = max(5, min(int(timeout_seconds or 30), 120))
        self._lock = threading.RLock()
        self._connection_service: Any = None

    def set_connection_service(self, service: Any) -> None:
        self._connection_service = service
        service.runtime_sync.tracks_remote = True
        service.set_managed_remote_identity_provider(self._managed_remote_identity)

    def _managed_remote_identity(self) -> dict[str, str]:
        return managed_remote_identity(self._default_remote_name(), self._config_path())

    def ensure_confirmation(self) -> dict[str, Any]:
        """Reconcile the Worker-local Remote before deriving a transfer descriptor.

        Only existing database credentials are used. A missing/legacy marker
        needs a real save and probe; a verified marker permits a read-only
        fast path. Neither path changes a connection revision or a job plan.
        """

        service = self._connection_service
        if service is None:
            raise RcloneWebdavConfigError(
                _AUTO_CONFIRMATION_MESSAGES["unbound"], 503, code="RCLONE_SYNC_RESULT_UNKNOWN",
            )
        sync = service.runtime_sync
        lock_timeout = min(15.0, float(self._timeout_seconds))
        try:
            with sync.mutation(timeout=lock_timeout) as token, sync.remote_lock.hold(lock_timeout), self._lock:
                rows = service.list_connections()
                if len(rows) != 1 or not rows[0].get("enabled"):
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["connection"], 409, code="RCLONE_SYNC_CONFLICT",
                    )
                current = rows[0]
                revision = int(current.get("revision") or 0)
                if revision <= 0:
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["credentials"], 409, code="RCLONE_SYNC_CONFLICT",
                    )
                remote = self._validate_remote_name(self._default_remote_name())
                config_path = self._config_path()
                fence = {
                    "connection_id": str(current["connection_id"]),
                    "revision": revision,
                    "operation_token": token,
                    "managed_remote": self._managed_remote_identity(),
                }
                self._assert_sync_fence(fence)
                credentials = service.resolve_credentials(fence["connection_id"], revision)
                if (
                    not credentials.get("enabled") or not credentials.get("password")
                    or str(credentials.get("base_url") or "").rstrip("/") != str(current.get("base_url") or "").rstrip("/")
                    or str(credentials.get("username") or "") != str(current.get("username") or "")
                ):
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["credentials"], 409, code="RCLONE_SYNC_CONFLICT",
                    )
                marker = sync.state()
                self._validate_auto_confirmation_marker(marker, fence["managed_remote"])
                configs = self._config_dump(config_path)
                self._assert_sync_fence(fence)
                state = self._status_from_configs(remote, configs)
                remote_config_fingerprint = self._remote_config_fingerprint(configs.get(remote), fence["managed_remote"])
                if remote in configs and (
                    not isinstance(configs[remote], Mapping)
                    or state["type"] != "webdav"
                    or str(state["url"]).rstrip("/") != str(credentials["base_url"]).rstrip("/")
                    or state["user"] != str(credentials["username"])
                ):
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["remote"], 409, code="RCLONE_SYNC_CONFLICT",
                    )
                fingerprint = service.runtime_fingerprint(
                    fence["connection_id"], revision, managed_remote=fence["managed_remote"],
                )
                if (
                    state["configured"] and state["password_set"]
                    and remote_config_fingerprint
                    and marker.get("phase") == "confirmed"
                    and marker.get("fingerprint") == fingerprint
                    and marker.get("managed_remote") == fence["managed_remote"]
                    and marker.get("remote_config_fingerprint") == remote_config_fingerprint
                    and service.runtime_identity_matches(fence["connection_id"], revision)
                ):
                    self._assert_sync_fence(fence)
                    return {
                        **state, "sync_status": "confirmed", "connection_status": "success",
                        "auto_confirmed": False, "remote_rebuilt": False,
                        "message": _AUTO_CONFIRMATION_MESSAGES["confirmed"],
                    }
                # Reuse the same operation token. The delegated save validates
                # revision/scope again, backs up the Remote, probes real rclone,
                # and only then commits its confirmed marker.
                result = self.save({
                    "remote_name": remote,
                    "url": credentials["base_url"],
                    "username": credentials["username"],
                    "password": credentials["password"],
                    "_openlist_sync": fence,
                })
                self._assert_sync_fence(fence)
                if not service.runtime_identity_matches(fence["connection_id"], revision):
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["unknown"], 503, code="RCLONE_SYNC_RESULT_UNKNOWN",
                    )
                result.pop("_openlist_sync", None)
                return {
                    **result, "sync_status": "confirmed", "connection_status": "success",
                    "auto_confirmed": True, "remote_rebuilt": not state["configured"],
                    "message": _AUTO_CONFIRMATION_MESSAGES["restored"],
                }
        except OpenListSyncConflict as exc:
            error = RcloneWebdavConfigError(str(exc), 409, code="RCLONE_SYNC_CONFLICT")
            error.retryable = True
            raise error from exc
        except RcloneWebdavConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - do not expose credentials from config/DB failures
            raise RcloneWebdavConfigError(
                _AUTO_CONFIRMATION_MESSAGES["unknown"], 503, code="RCLONE_SYNC_RESULT_UNKNOWN",
            ) from exc

    @staticmethod
    def _validate_auto_confirmation_marker(marker: dict[str, Any], managed_remote: dict[str, str]) -> None:
        if not marker:
            return
        # The runtime reader represents malformed JSON as {phase: unknown}.
        # Do not mistake that for a legitimate interrupted save marker.
        if (
            not isinstance(marker, dict)
            or marker.get("phase") not in {"confirmed", "pending", "unknown"}
            or not isinstance(marker.get("token"), str) or not marker["token"]
            or not isinstance(marker.get("fingerprint"), str)
            or not isinstance(marker.get("managed_remote", {}), dict)
            or not isinstance(marker.get("remote_config_fingerprint", ""), str)
        ):
            raise RcloneWebdavConfigError(
                _AUTO_CONFIRMATION_MESSAGES["marker"], 409, code="RCLONE_SYNC_CONFIRMATION_INVALID",
            )
        previous_scope = marker.get("managed_remote")
        if previous_scope and previous_scope != managed_remote:
            raise RcloneWebdavConfigError(
                _AUTO_CONFIRMATION_MESSAGES["scope"], 409, code="RCLONE_SYNC_CONFLICT",
            )

    @contextmanager
    def _read_guard(self):
        service = self._connection_service
        try:
            with (service.runtime_sync.remote_lock.hold() if service else nullcontext()), self._lock:
                yield
        except OpenListSyncConflict as exc:
            raise RcloneWebdavConfigError(str(exc), 409, code="RCLONE_SYNC_CONFLICT") from exc

    def _assert_sync_fence(self, fence: dict[str, Any] | None) -> None:
        if fence is None:
            return
        service = self._connection_service
        current = service.get_connection(fence["connection_id"])
        if not service.runtime_sync.owns(fence["operation_token"]) or not current or (
            int(current.get("revision") or 0) != fence["revision"]
        ) or fence.get("managed_remote") != self._managed_remote_identity():
            raise RcloneWebdavConfigError(
                "OpenList 同步执行权或修订已变化，旧请求不得继续写入或回滚",
                409, code="RCLONE_SYNC_RESULT_UNKNOWN",
            )

    @staticmethod
    def _remote_config_fingerprint(actual: Any, managed_remote: Mapping[str, str]) -> str:
        if not isinstance(actual, Mapping):
            return ""
        encoded = json.dumps({
            "managed_remote": dict(managed_remote),
            "type": str(actual.get("type") or "").strip().lower(),
            "url": str(actual.get("url") or "").rstrip("/"),
            "user": str(actual.get("user") or ""),
            "pass": str(actual.get("pass") or ""),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(b"fnos-rclone-remote-config-v1\0" + encoded).hexdigest()

    def _confirmation_status(self, actual: Mapping[str, Any], *, remote_config_fingerprint: str = "") -> str:
        service = self._connection_service
        try:
            marker = service.runtime_sync.state()
            if not marker:
                return "confirmation_required"
            if marker.get("phase") != "confirmed":
                return str(marker.get("phase") or "unknown")
            if not remote_config_fingerprint or marker.get("remote_config_fingerprint") != remote_config_fingerprint:
                return "confirmation_required"
            rows = service.list_connections()
            if len(rows) == 1 and (
                actual.get("remote_name") == self._default_remote_name()
                and actual.get("type") == "webdav"
                and actual.get("password_set") is True
                and str(actual.get("url") or "").rstrip("/") == str(rows[0].get("base_url") or "").rstrip("/")
                and str(actual.get("user") or "") == str(rows[0].get("username") or "")
                and service.runtime_identity_matches(rows[0]["connection_id"], rows[0]["revision"])
            ):
                return "confirmed"
        except Exception:
            pass
        return "unknown"

    @contextmanager
    def _save_guard(self, payload: Mapping[str, Any]):
        service = self._connection_service
        if service is None:
            yield dict(payload), None
            return
        supplied = payload.get("_openlist_sync")
        if supplied is not None and not isinstance(supplied, dict):
            raise RcloneWebdavConfigError("OpenList 同步凭证格式无效", 409, code="RCLONE_SYNC_CONFLICT")
        sync = service.runtime_sync
        # A delegated Worker must not reacquire the Web process' change lock.
        # Raw config endpoints own it themselves and cannot select other credentials.
        owner = nullcontext(str((supplied or {}).get("operation_token") or "")) if supplied is not None else sync.mutation()
        try:
            with owner as token, sync.remote_lock.hold():
                rows = service.list_connections()
                if len(rows) != 1:
                    raise RcloneWebdavConfigError("托管 Remote 必须对应唯一的 OpenList 连接", 409, code="RCLONE_SYNC_CONFLICT")
                current = rows[0]
                fence = {
                    "connection_id": str(current["connection_id"]),
                    "revision": int(current["revision"]),
                    "operation_token": token,
                    "managed_remote": self._managed_remote_identity(),
                }
                if supplied is not None and supplied != fence:
                    raise RcloneWebdavConfigError("OpenList 同步修订已过期", 409, code="RCLONE_SYNC_CONFLICT")
                self._assert_sync_fence(fence)
                credentials = service.resolve_credentials(fence["connection_id"], fence["revision"])
                remote = self._validate_remote_name(payload.get("remote_name") or self._default_remote_name())
                if remote != self._default_remote_name() or (
                    str(payload.get("url") or "").rstrip("/") != str(credentials["base_url"]).rstrip("/")
                    or str(payload.get("username") or "") != str(credentials["username"])
                    or (payload.get("password") not in (None, "") and not hmac.compare_digest(
                        str(payload["password"]).encode("utf-8"), str(credentials["password"]).encode("utf-8"),
                    ))
                ):
                    raise RcloneWebdavConfigError(
                        "请通过 OpenList 保存入口修改连接；Remote 不得使用另一套凭据",
                        409, code="RCLONE_SYNC_CONFLICT",
                    )
                if not sync.set_state(token, "pending", managed_remote=fence["managed_remote"]):
                    raise RcloneWebdavConfigError("OpenList 同步执行权已丢失", 409, code="RCLONE_SYNC_RESULT_UNKNOWN")
                yield {**payload, "password": credentials["password"]}, fence
        except OpenListSyncConflict as exc:
            raise RcloneWebdavConfigError(str(exc), 409, code="RCLONE_SYNC_CONFLICT") from exc
        except OSError as exc:
            raise RcloneWebdavConfigError("OpenList 同步确认状态无法持久化", 503, code="RCLONE_SYNC_RESULT_UNKNOWN") from exc

    def status(self, remote_name: Any = None) -> dict[str, Any]:
        with self._read_guard():
            remote = self._validate_remote_name(remote_name or self._default_remote_name())
            configs = self._config_dump(self._config_path())
            result = self._status_from_configs(remote, configs)
            if self._connection_service is not None:
                result["sync_status"] = self._confirmation_status(
                    result,
                    remote_config_fingerprint=self._remote_config_fingerprint(configs.get(remote), self._managed_remote_identity()),
                )
                if result["sync_status"] != "confirmed":
                    result["connection_status"] = "confirmation_required"
                    result["message"] = _AUTO_CONFIRMATION_MESSAGES["waiting"]
            return result

    @staticmethod
    def _status_from_configs(remote: str, configs: Mapping[str, Any]) -> dict[str, Any]:
        item = configs.get(remote)
        if not isinstance(item, Mapping):
            return {
                "success": True,
                "configured": False,
                "remote_name": remote,
                "type": "",
                "url": "",
                "user": "",
                "password_set": False,
                "connection_status": "unconfigured",
                "message": "WebDAV 尚未配置",
            }
        remote_type = str(item.get("type") or "").strip().lower()
        return {
            "success": True,
            "configured": True,
            "remote_name": remote,
            "type": remote_type,
            "url": str(item.get("url") or ""),
            "user": str(item.get("user") or ""),
            "password_set": bool(item.get("pass")),
            "connection_status": "unknown" if remote_type == "webdav" else "unsupported",
            "message": "WebDAV 已配置" if remote_type == "webdav" else f"同名 Remote 已存在，类型为 {remote_type or '未知'}",
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise RcloneWebdavConfigError("请求格式不正确")
        with self._save_guard(payload) as (values, fence):
            return self._save(values, fence=fence)

    def _save(self, payload: Mapping[str, Any], *, fence: dict[str, Any] | None) -> dict[str, Any]:
        remote = self._validate_remote_name(payload.get("remote_name") or self._default_remote_name())
        url = self._validate_url(payload.get("url"))
        username = self._validate_text(payload.get("username"), "用户名", max_length=255, required=True)
        password = self._validate_text(payload.get("password"), "密码", max_length=1024, required=False, trim=False)

        with self._lock:
            config_path = self._config_path()
            backup_path = self._backup_path(config_path)
            shell = self._shell()
            configs = self._config_dump(config_path)
            current = configs.get(remote)
            if isinstance(current, Mapping):
                remote_type = str(current.get("type") or "").strip().lower()
                if remote_type != "webdav":
                    raise RcloneWebdavConfigError(
                        f"Remote“{remote}”已存在且类型不是 WebDAV，未覆盖原配置",
                        409,
                    )
                operation = "update"
                if not password and not current.get("pass"):
                    raise RcloneWebdavConfigError("当前 Remote 没有已保存密码，请填写密码")
            else:
                operation = "create"
                if not password:
                    raise RcloneWebdavConfigError("首次配置 WebDAV 时必须填写密码")

            self._backup(config_path, backup_path, shell)
            secret_values = [password] if password else []
            try:
                self._assert_sync_fence(fence)
                result = self._run(
                    [shell, "-c", self._SAVE_SCRIPT],
                    input_text="\n".join(
                        (config_path, operation, remote, url, username, password)
                    )
                    + "\n",
                )
                self._assert_sync_fence(fence)
                if result.returncode != 0:
                    detail = self._safe_process_message(result, secret_values)
                    message = "rclone 配置写入失败，请确认配置目录权限正常"
                    raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 502)
                self._test_remote(
                    remote,
                    config_path=config_path,
                    secret_values=secret_values,
                )
                saved_configs = self._config_dump(config_path)
                state = self._status_from_configs(remote, saved_configs)
                self._assert_sync_fence(fence)
                if (
                    state.get("url", "").rstrip("/") != url or state.get("user") != username
                    or state.get("type") != "webdav" or state.get("password_set") is not True
                ):
                    raise RcloneWebdavConfigError("Remote 写入后身份校验不一致", 409, code="RCLONE_SYNC_RESULT_UNKNOWN")
            except RcloneWebdavConfigError as exc:
                if fence is not None:
                    self._assert_sync_fence(fence)
                    if exc.code == "RCLONE_SYNC_RESULT_UNKNOWN":
                        raise
                restored = self._restore(config_path, backup_path, shell)
                suffix = "，已恢复保存前的配置" if restored else "，且自动恢复失败，请检查 rclone 配置文件"
                code = exc.code or "RCLONE_WEBDAV_SYNC_FAILED"
                if fence is not None:
                    self._assert_sync_fence(fence)
                    self._connection_service.runtime_sync.set_state(fence["operation_token"], "unknown")
                    if not restored:
                        code = "RCLONE_SYNC_RESULT_UNKNOWN"
                raise RcloneWebdavConfigError(f"{exc}{suffix}", exc.status_code, code=code) from exc

            if fence is not None:
                remote_config_fingerprint = self._remote_config_fingerprint(saved_configs.get(remote), fence["managed_remote"])
                fingerprint = self._connection_service.runtime_fingerprint(
                    fence["connection_id"], fence["revision"], managed_remote=fence["managed_remote"],
                )
                if not self._connection_service.runtime_sync.set_state(
                    fence["operation_token"], "confirmed", fingerprint=fingerprint, managed_remote=fence["managed_remote"],
                    remote_config_fingerprint=remote_config_fingerprint,
                ):
                    raise RcloneWebdavConfigError("OpenList 同步执行权已丢失", 409, code="RCLONE_SYNC_RESULT_UNKNOWN")
                state["_openlist_sync"] = dict(fence)
                state["remote_config_fingerprint"] = remote_config_fingerprint

            state.update(
                {
                    "connection_status": "success",
                    "message": "WebDAV 配置已保存，连接检测成功",
                }
            )
            return state

    def test(self, remote_name: Any = None) -> dict[str, Any]:
        remote = self._validate_remote_name(remote_name or self._default_remote_name())
        with self._read_guard():
            config_path = self._config_path()
            configs = self._config_dump(config_path)
            state = self._status_from_configs(remote, configs)
            if not state["configured"]:
                raise RcloneWebdavConfigError("请先保存 WebDAV 配置")
            if state["type"] != "webdav":
                raise RcloneWebdavConfigError(f"Remote“{remote}”不是 WebDAV 类型", 409)
            if self._connection_service is not None:
                if self._confirmation_status(
                    state,
                    remote_config_fingerprint=self._remote_config_fingerprint(configs.get(remote), self._managed_remote_identity()),
                ) != "confirmed":
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["waiting"],
                        409, code="RCLONE_SYNC_RESULT_UNKNOWN",
                    )
            self._test_remote(remote, config_path=config_path)
            if self._connection_service is not None:
                configs = self._config_dump(config_path)
                state = self._status_from_configs(remote, configs)
                if self._confirmation_status(
                    state,
                    remote_config_fingerprint=self._remote_config_fingerprint(configs.get(remote), self._managed_remote_identity()),
                ) != "confirmed":
                    raise RcloneWebdavConfigError(
                        _AUTO_CONFIRMATION_MESSAGES["changed"],
                        409, code="RCLONE_SYNC_RESULT_UNKNOWN",
                    )
        state.update({"connection_status": "success", "message": "WebDAV 连接成功"})
        return state

    def _config_dump(self, config_path: str) -> dict[str, Any]:
        result = self._run(["rclone", "config", "dump", "--config", config_path])
        if result.returncode != 0:
            detail = self._safe_process_message(result, [])
            message = "无法读取 rclone 配置，请确认 rclone 可用且配置目录权限正常"
            raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 503)
        try:
            data = json.loads(result.stdout or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RcloneWebdavConfigError("rclone 配置格式无法识别，请检查配置文件", 502) from exc
        if not isinstance(data, dict):
            raise RcloneWebdavConfigError("rclone 配置格式无法识别，请检查配置文件", 502)
        return data

    def _test_remote(
        self,
        remote: str,
        *,
        config_path: str,
        secret_values: list[str] | None = None,
    ) -> None:
        result = self._run(
            ["rclone", "lsd", f"{remote}:", "--config", config_path]
        )
        if result.returncode == 0:
            return
        detail = self._safe_process_message(result, secret_values or [])
        message = "WebDAV 连接失败"
        if detail:
            message = f"{message}：{detail}"
        raise RcloneWebdavConfigError(message, 502)

    def _backup(self, config_path: str, backup_path: str, shell: str) -> None:
        result = self._run(
            [shell, "-c", self._BACKUP_SCRIPT],
            input_text=f"{config_path}\n{backup_path}\n",
        )
        if result.returncode != 0:
            detail = self._safe_process_message(result, [])
            message = "无法备份 rclone 配置，已停止保存"
            raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 503)

    def _restore(self, config_path: str, backup_path: str, shell: str) -> bool:
        try:
            result = self._run(
                [shell, "-c", self._RESTORE_SCRIPT],
                input_text=f"{config_path}\n{backup_path}\n",
            )
        except RcloneWebdavConfigError:
            return False
        return result.returncode == 0

    def _run(self, command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RcloneWebdavConfigError(
                "rclone 管理命令不可用",
                503,
                code=RCLONE_UNAVAILABLE,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneWebdavConfigError(
                _AUTO_CONFIRMATION_MESSAGES["timeout"],
                504, code="RCLONE_SYNC_RESULT_UNKNOWN",
            ) from exc
        except OSError as exc:
            raise RcloneWebdavConfigError(
                "无法调用 rclone 管理配置",
                503,
                code=RCLONE_UNAVAILABLE,
            ) from exc

    def _default_remote_name(self) -> str:
        return str(self._current_config().get("remote_name") or "MP")

    def _config_path(self) -> str:
        return self._validate_text(
            self._current_config().get("config_path") or self.CONFIG_PATH,
            "rclone 配置路径",
            max_length=4096,
            required=True,
        )

    @staticmethod
    def _backup_path(config_path: str) -> str:
        return f"{config_path}.webui.bak"

    def _shell(self) -> str:
        return self._validate_text(
            self._current_config().get("shell") or "sh",
            "rclone Worker shell",
            max_length=4096,
            required=True,
        )

    def _validate_remote_name(self, value: Any) -> str:
        remote = self._validate_text(value, "Remote 名称", max_length=64, required=True)
        if any(char in remote for char in (":", "/", "\\")):
            raise RcloneWebdavConfigError("Remote 名称不能包含冒号或路径分隔符")
        return remote

    def _validate_url(self, value: Any) -> str:
        url = self._validate_text(value, "WebDAV URL", max_length=2048, required=True)
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise RcloneWebdavConfigError("WebDAV URL 必须是完整的 http 或 https 地址")
        if parsed.username is not None or parsed.password is not None:
            raise RcloneWebdavConfigError("WebDAV URL 中不要包含用户名或密码")
        return url.rstrip("/")

    def _validate_text(
        self,
        value: Any,
        label: str,
        *,
        max_length: int,
        required: bool,
        trim: bool = True,
    ) -> str:
        text = "" if value is None else str(value)
        if trim:
            text = text.strip()
        if required and not text:
            raise RcloneWebdavConfigError(f"请填写{label}")
        if len(text) > max_length:
            raise RcloneWebdavConfigError(f"{label}长度不能超过 {max_length} 个字符")
        if self._CONTROL_PATTERN.search(text):
            raise RcloneWebdavConfigError(f"{label}不能包含控制字符")
        return text

    @classmethod
    def _safe_process_message(cls, result: subprocess.CompletedProcess[str], secrets: list[str]) -> str:
        raw = str(result.stderr or result.stdout or "").strip().splitlines()
        message = raw[-1].strip()[:500] if raw else ""
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
        return cls._CONTROL_PATTERN.sub("", message)
