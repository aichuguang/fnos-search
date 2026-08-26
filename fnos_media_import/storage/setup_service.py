"""面向管理后台的多链路聚合配置服务。

底层仍保留账号修订、OpenList mount、目标 binding 与来源 assignment，管理端只
操作账号、保存位置和来源规则三个产品概念。本服务负责生成内部 ID、分类定位器
和默认路径，避免前端编排多个领域对象。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import secrets
import logging
import time
from dataclasses import asdict
from typing import Any

from ..config import CATEGORY_KEYS
from ..constants import CATEGORY_LABELS
from ..domain.models import LocalLocator, NativeLocator, OpenListLocator, RcloneLocator, TargetCategory
from ..time_utils import utc_now_iso


logger = logging.getLogger("fnos_media_import.storage_setup")


class StorageSetupError(ValueError):
    code = "STORAGE_SETUP_INVALID"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        field_errors: dict[str, str] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(str(message))
        if code:
            self.code = str(code)
        self.status = int(status) if status is not None else None
        self.field_errors = dict(field_errors or {})
        self.retryable = bool(retryable)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _path_join(root: str, leaf: str) -> str:
    root_text = _text(root).replace("\\", "/").strip("/")
    leaf_text = _text(leaf).replace("\\", "/").strip("/")
    return "/".join(part for part in (root_text, leaf_text) if part)


def _generated_id(prefix: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", _text(name).lower()).strip("-")[:28]
    suffix = secrets.token_hex(4)
    return f"{prefix}_{slug + '_' if slug else ''}{suffix}"


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).lower() in {"1", "true", "yes", "on"}


class StorageSetupService:
    """把底层多表配置投影为三个可直接操作的管理工作区。"""

    def __init__(
        self,
        *,
        accounts_service: Any,
        connection_service: Any,
        mount_service: Any,
        target_service: Any,
        assignment_service: Any,
        connection_factory: Any,
        rclone_webdav_sync: Any = None,
        rclone_webdav_test: Any = None,
        organizer_enabled: Any = None,
        openlist_api_factory: Any = None,
    ) -> None:
        self.accounts = accounts_service
        self.connections = connection_service
        self.mounts = mount_service
        self.targets = target_service
        self.assignments = assignment_service
        self.connection_factory = connection_factory
        self.rclone_webdav_sync = rclone_webdav_sync
        self.rclone_webdav_test = rclone_webdav_test
        self.organizer_enabled = organizer_enabled
        # Optional OpenList JSON-API client used only to invalidate directory
        # caches.  WebDAV has no refresh verb, so without this the observer can
        # only wait out a cache it cannot expire.  Absent factory degrades to
        # plain polling rather than failing setup.
        self.openlist_api_factory = openlist_api_factory
        self.provider_uploader_resolver: Any = None
        self.provider_folder_resolver: Any = None

    def set_provider_uploader_resolver(self, resolver: Any) -> None:
        """Inject the account-revision-scoped native uploader factory."""

        self.provider_uploader_resolver = resolver

    def set_provider_folder_resolver(self, resolver: Any) -> None:
        """Inject an account-revision-scoped native folder browser."""

        self.provider_folder_resolver = resolver

    @staticmethod
    def _safe_rclone_message(exc: BaseException, secrets_to_redact: tuple[str, ...]) -> str:
        payload = getattr(exc, "payload", None)
        message = ""
        if isinstance(payload, dict):
            message = _text(payload.get("message"))
        if not message:
            message = _text(exc)
        for secret in secrets_to_redact:
            if secret:
                message = message.replace(secret, "***")
        return message[:500]

    _RCLONE_ABSENT_CODES = frozenset(
        {"RCLONE_UNAVAILABLE", "WORKER_UNAVAILABLE", "WORKER_TIMEOUT"}
    )

    @classmethod
    def _rclone_plane_absent_payload(cls, payload: dict[str, Any]) -> bool:
        codes = {
            _text(payload.get("error_code")).upper(),
            _text(payload.get("status")).upper(),
            _text(payload.get("code")).upper(),
        }
        return bool(codes & cls._RCLONE_ABSENT_CODES)

    @classmethod
    def _rclone_plane_absent(cls, exc: BaseException) -> bool:
        """Tell "no rclone here at all" apart from "rclone refused the request".

        rclone only moves bytes between two different providers.  An OpenList
        connection is also what drives Organizer and STRM, so a deployment with
        no rclone binary and no reachable worker must still be able to save one.
        Only a genuine rejection (bad credentials, a hijacked remote name, a
        write that failed) may keep failing the save.
        """

        if _text(getattr(exc, "code", "")).upper() in cls._RCLONE_ABSENT_CODES:
            return True
        payload = getattr(exc, "payload", None)
        return isinstance(payload, dict) and cls._rclone_plane_absent_payload(payload)

    @staticmethod
    def _rclone_degraded_state(message: str) -> dict[str, Any]:
        return {
            "configured": False,
            "available": False,
            "connection_status": "unavailable",
            "message": message,
        }

    def _sync_rclone_webdav(self, connection_id: str) -> dict[str, Any]:
        """Persist and probe the worker-local managed WebDAV remote.

        OpenList credentials stay server-side: the browser saves them once in
        the encrypted connection revision, then this bridge resolves that
        revision and forwards it directly to the isolated rclone worker.

        A missing rclone runtime downgrades to an unconfigured remote rather
        than failing the save; ``preflight`` still reports it as a hard failure
        for any route that actually needs a cross-provider copy.
        """

        if not callable(self.rclone_webdav_sync):
            return self._rclone_degraded_state(
                "当前部署没有可用的 rclone，已跳过 rclone Remote 同步；跨盘搬运不可用"
            )
        connection = self.connections.get_connection(connection_id)
        if connection is None:
            raise StorageSetupError(
                "OpenList 连接不存在，无法同步 rclone",
                code="OPENLIST_NOT_FOUND",
                status=404,
            )
        credentials = self.connections.resolve_credentials(
            connection_id,
            int(connection.get("revision") or 1),
        )
        password = str(credentials.get("password") or "")
        payload = {
            "url": credentials.get("base_url"),
            "username": credentials.get("username"),
            "password": password,
        }
        try:
            result = self.rclone_webdav_sync(payload)
        except StorageSetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize worker/control-plane errors
            status = int(
                getattr(exc, "status_code", None)
                or getattr(exc, "status", None)
                or 503
            )
            message = self._safe_rclone_message(
                exc,
                (
                    password,
                    str(credentials.get("username") or ""),
                ),
            )
            if self._rclone_plane_absent(exc):
                logger.warning(
                    "event=openlist_saved_without_rclone connection_id=%s reason=%s",
                    connection_id,
                    message or "rclone 不可用",
                )
                return self._rclone_degraded_state(
                    f"OpenList 已保存，但 rclone 不可用，跨盘搬运暂不可用：{message or 'rclone 管理命令不可用'}"
                )
            raise StorageSetupError(
                f"rclone WebDAV 同步或连接检测失败：{message or 'Worker 不可用'}",
                code="RCLONE_WEBDAV_SYNC_FAILED",
                status=status,
                retryable=status >= 500,
            ) from exc
        if not isinstance(result, dict) or not bool(result.get("success", True)):
            message = _text((result or {}).get("message")) if isinstance(result, dict) else ""
            if isinstance(result, dict) and self._rclone_plane_absent_payload(result):
                logger.warning(
                    "event=openlist_saved_without_rclone connection_id=%s reason=%s",
                    connection_id,
                    message or "rclone Worker 不可用",
                )
                return self._rclone_degraded_state(
                    f"OpenList 已保存，但 rclone 不可用，跨盘搬运暂不可用：{message or 'rclone Worker 不可用'}"
                )
            raise StorageSetupError(
                f"rclone WebDAV 同步或连接检测失败：{message or '返回结果无效'}",
                code="RCLONE_WEBDAV_SYNC_FAILED",
                status=503,
                retryable=True,
            )
        expected_url = _text(credentials.get("base_url")).rstrip("/")
        actual_url = _text(result.get("url")).rstrip("/")
        expected_user = _text(credentials.get("username"))
        actual_user = _text(result.get("user"))
        if (actual_url and actual_url != expected_url) or (
            actual_user and actual_user != expected_user
        ):
            raise StorageSetupError(
                "rclone WebDAV 已连接，但 Remote 与当前 OpenList 配置不一致",
                code="RCLONE_WEBDAV_SYNC_MISMATCH",
                status=409,
                retryable=False,
            )
        return {
            key: result.get(key)
            for key in (
                "remote_name",
                "configured",
                "type",
                "url",
                "user",
                "password_set",
                "connection_status",
                "message",
            )
            if key in result
        }

    def _probe_rclone_webdav(self, connection_id: str) -> dict[str, Any]:
        if not callable(self.rclone_webdav_test):
            raise StorageSetupError(
                "rclone WebDAV 检测服务尚未装配",
                code="RCLONE_WEBDAV_PROBE_UNAVAILABLE",
                status=503,
                retryable=True,
            )
        connection = self.connections.get_connection(connection_id)
        if connection is None:
            raise StorageSetupError(
                "OpenList 连接不存在，无法检测 rclone",
                code="OPENLIST_NOT_FOUND",
                status=404,
            )
        try:
            result = self.rclone_webdav_test()
        except StorageSetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize worker/control-plane errors
            status = int(
                getattr(exc, "status_code", None)
                or getattr(exc, "status", None)
                or 503
            )
            raise StorageSetupError(
                f"rclone WebDAV 连接检测失败：{self._safe_rclone_message(exc, ()) or 'Worker 不可用'}",
                code="RCLONE_WEBDAV_PROBE_FAILED",
                status=status,
                retryable=status >= 500,
            ) from exc
        if not isinstance(result, dict) or not bool(result.get("success", True)):
            raise StorageSetupError(
                f"rclone WebDAV 连接检测失败：{_text((result or {}).get('message')) or '返回结果无效'}",
                code="RCLONE_WEBDAV_PROBE_FAILED",
                status=503,
                retryable=True,
            )
        expected_url = _text(connection.get("base_url")).rstrip("/")
        actual_url = _text(result.get("url")).rstrip("/")
        expected_user = _text(connection.get("username"))
        actual_user = _text(result.get("user"))
        if actual_url != expected_url or actual_user != expected_user:
            raise StorageSetupError(
                "rclone Remote 与当前 OpenList 连接不一致，请重新保存 OpenList",
                code="RCLONE_WEBDAV_SYNC_MISMATCH",
                status=409,
            )
        return result

    def _provider_catalog_item(self, provider_kind: str) -> dict[str, Any]:
        normalized = _text(provider_kind).lower()
        return next(
            (
                dict(item)
                for item in self.accounts.catalog()
                if _text(item.get("provider_kind")).lower() == normalized
            ),
            {},
        )

    def _supports_relay_upload(self, provider_kind: str) -> bool:
        provider = self._provider_catalog_item(provider_kind)
        return bool((provider.get("capabilities") or {}).get("supports_relay_upload"))

    def _mount_native_proofs(
        self,
        provider_kind: str,
        mount: dict[str, Any] | None,
    ) -> tuple[bool, bool]:
        """Return frozen native-upload and Organizer mutation proof state.

        Every relay-upload Provider, including Mobile and Tianyi, writes the
        destination through its native API.  Therefore both native upload and
        Organizer mutation must be explicitly proven for the frozen account,
        connection and mount revision.  WebDAV writability is never a native
        upload proof.
        """

        value = dict(mount or {})
        native_locator = dict(value.get("native_locator") or {})
        relay_upload_capable = self._supports_relay_upload(provider_kind)
        upload_verified = bool(native_locator.get("upload_verified"))
        organizer_verified = bool(
            native_locator.get("organizer_mutation_verified")
            or (not relay_upload_capable and value.get("writable"))
        )
        return upload_verified, organizer_verified

    def _provider_uploader(
        self,
        provider_kind: str,
        account_id: str,
        revision: int,
    ) -> Any:
        if not callable(self.provider_uploader_resolver):
            raise StorageSetupError(
                "Provider 原生上传运行时尚未装配",
                code="PROVIDER_UPLOAD_UNAVAILABLE",
                status=503,
            )
        try:
            uploader = self.provider_uploader_resolver(
                _text(provider_kind),
                _text(account_id),
                int(revision or 0),
            )
        except StorageSetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize composition failures
            raise StorageSetupError(
                "Provider 原生上传器初始化失败",
                code="PROVIDER_UPLOAD_UNAVAILABLE",
                status=503,
                retryable=True,
            ) from exc
        if uploader is None:
            raise StorageSetupError(
                "所选 Provider 未提供原生上传器",
                code="PROVIDER_UPLOAD_UNAVAILABLE",
                status=503,
            )
        return uploader

    def list_account_folders(
        self,
        account_id: str,
        parent_id: str = "",
    ) -> dict[str, Any]:
        account = self.accounts.get_account(_text(account_id))
        if account is None or not account.enabled:
            raise StorageSetupError(
                "云盘账号不存在或已停用",
                code="ACCOUNT_NOT_FOUND",
                status=404,
            )
        revision = int(getattr(account, "current_revision", 0) or 0)
        if not callable(self.provider_folder_resolver):
            raise StorageSetupError(
                "Provider 原生目录浏览运行时尚未装配",
                code="PROVIDER_FOLDER_BROWSER_UNAVAILABLE",
                status=503,
            )
        try:
            browser = self.provider_folder_resolver(
                account.provider_kind,
                account.account_id,
                revision,
            )
        except Exception as exc:  # noqa: BLE001 - normalize composition failures
            raise StorageSetupError(
                "Provider 原生目录浏览器初始化失败",
                code="PROVIDER_FOLDER_BROWSER_UNAVAILABLE",
                status=503,
                retryable=True,
            ) from exc
        list_folders = getattr(browser, "list_folders", None)
        if not callable(list_folders):
            raise StorageSetupError(
                "所选 Provider 不支持原生目录浏览",
                code="PROVIDER_FOLDER_BROWSER_UNAVAILABLE",
                status=422,
            )
        selected_parent = _text(parent_id) or "-11"
        try:
            items = list_folders(selected_parent)
        except StorageSetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize Provider failures
            provider_code = _text(getattr(exc, "code", ""))
            try:
                provider_status = int(getattr(exc, "http_status", 0) or 0)
            except (TypeError, ValueError):
                provider_status = 0
            auth_failed = provider_code in {
                "AUTH_EXPIRED",
                "InvalidAccessToken",
                "InvalidSessionKey",
                "SESSION_CREDENTIALS_MISSING",
                "SESSION_IP_MISMATCH",
                "UserInvalidOpenToken",
            } or provider_status in {401, 403}
            raise StorageSetupError(
                (
                    "云盘认证已失效，请先重新登录账号"
                    if auth_failed
                    else f"原生目录读取失败：{exc}"
                ),
                code=("PROVIDER_AUTH_EXPIRED" if auth_failed else "PROVIDER_FOLDER_LIST_FAILED"),
                status=(401 if auth_failed else 503),
                retryable=not auth_failed,
            ) from exc
        return {
            "success": True,
            "account_id": account.account_id,
            "provider_kind": account.provider_kind,
            "account_revision": revision,
            "parent_id": selected_parent,
            "items": [dict(item) for item in items if isinstance(item, dict)],
        }

    # ------------------------------------------------------------------ read

    def snapshot(self) -> dict[str, Any]:
        selections = self.accounts.list_active_selections()
        accounts = []
        for account in self.accounts.list_accounts():
            item = self.accounts.serialize_account(account)
            selection = selections.get(f"{account.provider_kind}:default")
            item["active"] = bool(selection and selection.get("account_id") == account.account_id)
            accounts.append(item)

        connections = self.connections.list_connections()
        mounts = self.mounts.list_mounts()
        mount_by_id = {item["mount_id"]: item for item in mounts}
        account_by_id = {item["account_id"]: item for item in accounts}
        locations = [
            self._location_view(target, mount_by_id, account_by_id)
            for target in self.targets.list_targets()
        ]
        matrix = self.assignments.matrix()
        routes = self._route_view(matrix, locations)

        sources = self.assignments.sources()
        required_provider_kinds = {item["provider_kind"] for item in sources.values()}
        account_by_id = {item["account_id"]: item for item in accounts}
        account_ready = all(
            (selection := selections.get(f"{kind}:default")) is not None
            and bool((selected := account_by_id.get(selection.get("account_id"))) and selected.get("enabled"))
            and selected.get("last_test_status") == "ok"
            for kind in required_provider_kinds
        )
        openlist_ready = any(item.get("enabled") and item.get("last_test_status") == "ok" for item in connections)
        location_ready = any(item.get("ready") for item in locations)
        routing_ready = bool(matrix.get("rows")) and all(
            cell.get("ready")
            for row in matrix.get("rows", [])
            for cell in row.get("assignments", [])
        )
        completed = sum((account_ready, openlist_ready, location_ready, routing_ready))
        setup_ready = all((account_ready, openlist_ready, location_ready, routing_ready))
        providers = self.accounts.catalog()
        provider_by_kind = {
            str(item.get("provider_kind") or ""): item for item in providers
        }
        for location in locations:
            location["eligible_source_ids"] = [
                source_id
                for source_id, source in sources.items()
                if self._route_supported(source_id, source, location, provider_by_kind)
            ]
        return {
            "success": True,
            "providers": providers,
            "accounts": accounts,
            "connections": connections,
            "locations": locations,
            "routes": routes,
            "categories": [
                {"key": key, "label": CATEGORY_LABELS.get(key, key)} for key in CATEGORY_KEYS
            ],
            "progress": {
                "completed": completed,
                "total": 4,
                "ready": setup_ready,
                "steps": [
                    {"key": "accounts", "label": "云盘账号", "complete": account_ready},
                    {"key": "openlist", "label": "连接 OpenList", "complete": openlist_ready},
                    {"key": "locations", "label": "保存位置", "complete": location_ready},
                    {"key": "routes", "label": "入库规则", "complete": routing_ready},
                ],
            },
        }

    @staticmethod
    def _route_supported(
        source_id: str,
        source: dict[str, Any],
        target: dict[str, Any],
        providers: dict[str, dict[str, Any]],
    ) -> bool:
        source_kind = str(source.get("provider_kind") or "")
        target_kind = str(target.get("provider_kind") or "")
        source_caps = dict((providers.get(source_kind) or {}).get("capabilities") or {})
        target_caps = dict((providers.get(target_kind) or {}).get("capabilities") or {})
        if source_id == "sixpan_magnet" or source.get("source_form") == "magnet":
            return target_kind == source_kind and bool(target_caps.get("supports_native_offline"))
        if source_kind == target_kind:
            return bool(source_caps.get("supports_native_save"))
        if not source_caps.get("supports_source_materialization"):
            return False
        if target_caps.get("supports_relay_upload"):
            return bool(
                target.get("native_upload_verified")
                and target.get("organizer_mutation_verified")
            )
        if target.get("kind") == "local":
            return True
        # Cross-provider copies into OpenList ultimately upload files through
        # WebDAV/rclone. A mount whose current PUT probe fails (OpenList can
        # collapse an underlying driver upload error into HTTP 405) must never
        # be offered for that route, even when native same-provider save is
        # still usable.
        webdav_writable = bool(target.get("webdav_writable", True))
        if target.get("kind") == "generic":
            return webdav_writable
        return bool(target_caps.get("supports_rclone_dest")) and webdav_writable

    def _webdav_file_write_required(self, provider_kind: str) -> bool:
        policy = getattr(self.mounts, "requires_webdav_file_write_for_provider", None)
        if callable(policy):
            return bool(policy(provider_kind))
        return True

    def _native_locator_with_probe(
        self,
        *,
        provider_kind: str,
        account: Any,
        locator_kind: str,
        locator_value: str,
        display_name: str,
        connection_id: str,
        webdav_root: str,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        native_locator: dict[str, Any] = {
            "kind": _text(locator_kind),
            "value": _text(locator_value),
        }
        previous = dict(previous or {})
        if display_name:
            native_locator["display_name"] = _text(display_name)
        elif (
            _text(previous.get("kind")) == native_locator["kind"]
            and _text(previous.get("value")) == native_locator["value"]
            and _text(previous.get("display_name"))
        ):
            native_locator["display_name"] = _text(previous.get("display_name"))

        if not self._supports_relay_upload(provider_kind):
            return native_locator
        if account is None:
            raise StorageSetupError(
                "Provider 原生上传探测缺少账号",
                code="PROVIDER_UPLOAD_UNAVAILABLE",
                status=503,
            )
        connection = self.connections.get_connection(connection_id) or {}
        fingerprint_source = {
            "provider_kind": _text(provider_kind).lower(),
            "account_id": _text(account.account_id),
            "account_revision": int(account.current_revision or 0),
            "locator_kind": native_locator["kind"],
            "locator_value": native_locator["value"],
            "connection_id": _text(connection_id),
            "connection_revision": int(connection.get("revision") or 0),
            "webdav_root": posixpath.normpath(
                "/" + _text(webdav_root).replace("\\", "/").strip("/")
            ),
        }
        probe_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            _text(previous.get("probe_fingerprint")) == probe_fingerprint
            and previous.get("upload_verified")
            and previous.get("organizer_mutation_verified")
        ):
            native_locator.update(
                {
                    "upload_verified": True,
                    "upload_tested_at": _text(previous.get("upload_tested_at")),
                    "organizer_mutation_verified": True,
                    "organizer_tested_at": _text(previous.get("organizer_tested_at")),
                    "probe_message": _text(previous.get("probe_message")),
                    "probe_fingerprint": probe_fingerprint,
                }
            )
            return {key: value for key, value in native_locator.items() if value not in (None, "")}

        uploader = self._provider_uploader(
            provider_kind,
            account.account_id,
            int(account.current_revision or 0),
        )
        probe = getattr(uploader, "probe_target", None)
        if not callable(probe):
            raise StorageSetupError(
                "所选 Provider 未实现原生上传探测",
                code="PROVIDER_UPLOAD_PROBE_UNAVAILABLE",
                status=503,
            )
        observer = self._openlist_mutation_observer(connection_id, webdav_root)
        result = probe(
            target_locator={
                "kind": native_locator["kind"],
                "value": native_locator["value"],
            },
            observer=observer,
        )
        result = dict(result or {}) if isinstance(result, dict) else {}
        if (
            not result.get("ok")
            or not result.get("upload_verified")
            or not result.get("organizer_mutation_verified")
        ):
            probe_code = str(result.get("code") or "").strip()
            probe_message = str(result.get("message") or "").strip()
            # 探测失败最常见的原因是 webdav_root 与 native_root 指向了两个不同的
            # 目录：原生 API 传上去了，OpenList 那侧却在另一个目录里找不到。两个
            # 字段各自独立填写、后端不做一致性校验，所以把 OpenList 目录、原生
            # 定位符和真实探测路径打在同一行，看日志就能判断是不是配对错了。
            probe_paths = dict(getattr(observer, "probe_paths", None) or {})
            logger.warning(
                "event=storage_native_probe_failed provider=%s account_id=%s "
                "connection_id=%s webdav_root=%s native_locator_kind=%s "
                "native_locator_value=%s probe_root=%s probe_source_path=%s "
                "probe_moved_path=%s upload_verified=%s organizer_mutation_verified=%s "
                "code=%s message=%s",
                provider_kind,
                account.account_id,
                connection_id,
                webdav_root,
                native_locator["kind"],
                native_locator["value"],
                probe_paths.get("root") or "",
                probe_paths.get("source") or "",
                probe_paths.get("moved") or "",
                bool(result.get("upload_verified")),
                bool(result.get("organizer_mutation_verified")),
                probe_code,
                probe_message,
            )
            auth_failed = probe_code in {
                "AUTH_EXPIRED",
                "InvalidSessionKey",
                "InvalidAccessToken",
                "SESSION_IP_MISMATCH",
                "SESSION_CREDENTIALS_MISSING",
                "UserInvalidOpenToken",
            } or "认证已失效" in probe_message or "authentication expired" in probe_message.lower()
            quota_failed = probe_code in {
                "InsufficientStorageSpace",
                "INSUFFICIENTSTORAGESPACE",
            } or "空间已用尽" in probe_message or "free space has been used up" in probe_message.lower()
            if auth_failed:
                raise StorageSetupError(
                    "云盘认证已失效，请先重新登录账号后重试",
                    code="PROVIDER_AUTH_EXPIRED",
                    status=401,
                    retryable=False,
                    field_errors={"native_root": "云盘认证已失效，请先重新登录账号"},
                )
            if quota_failed:
                raise StorageSetupError(
                    probe_message or "云盘存储空间不足",
                    code="PROVIDER_STORAGE_INSUFFICIENT",
                    status=409,
                    retryable=False,
                    field_errors={"native_root": "云盘存储空间不足，请清理空间后重试"},
                )
            raise StorageSetupError(
                _text(result.get("message")) or "Provider 原生上传与整理链路检测未通过",
                code=_text(result.get("code")) or "PROVIDER_UPLOAD_PROBE_FAILED",
                status=503,
                retryable=True,
                field_errors={"native_root": "原生上传或 OpenList 整理检测未通过"},
            )
        tested_at = utc_now_iso()
        native_locator.update(
            {
                "upload_verified": True,
                "upload_tested_at": tested_at,
                "organizer_mutation_verified": True,
                "organizer_tested_at": tested_at,
                "probe_message": _text(result.get("message")),
                "probe_fingerprint": probe_fingerprint,
            }
        )
        return native_locator

    def _openlist_dir_refresher(self, credentials: dict[str, Any]) -> Any:
        """Return a best-effort OpenList directory cache invalidator.

        WebDAV exposes no refresh verb, so cache invalidation has to go through
        the JSON API (``/api/fs/list`` with ``refresh=true``).  Every failure
        here is swallowed: refreshing is an optimisation over waiting, and the
        caller still has its own polling window to fall back on.
        """

        factory = self.openlist_api_factory
        if not callable(factory):
            return lambda _path: None
        try:
            client = factory(dict(credentials or {}))
        except Exception:  # noqa: BLE001 - refresh stays optional
            return lambda _path: None
        lister = getattr(client, "list_dir", None)
        if not callable(lister):
            return lambda _path: None

        def refresh(path: str) -> None:
            try:
                lister(path, refresh=True)
            except Exception:  # noqa: BLE001 - refresh stays optional
                pass

        return refresh

    def _openlist_mutation_observer(
        self,
        connection_id: str,
        webdav_root: str,
    ) -> Any:
        connection = self.connections.get_connection(connection_id)
        revision = int((connection or {}).get("revision") or 0)
        if connection is None or revision <= 0:
            raise StorageSetupError(
                "OpenList 连接不存在或缺少 revision",
                code="OPENLIST_NOT_FOUND",
                status=404,
            )
        credentials = self.connections.resolve_credentials(connection_id, revision)
        client = self.mounts.webdav_factory(credentials)
        root = posixpath.normpath("/" + _text(webdav_root).strip("/"))
        refresh_dir = self._openlist_dir_refresher(credentials)
        # The observer knows the real probe paths, the caller knows the native
        # locator they were meant to pair with.  Publish the paths so the
        # failure log in ``_probe_native_target`` can print both halves on one
        # line — a wrong pairing is then visible from the log alone.
        probe_paths: dict[str, str] = {"root": root}

        def observe(info: dict[str, Any]) -> dict[str, Any]:
            file_name = posixpath.basename(_text(info.get("file_name")))
            if not file_name:
                return {
                    "organizer_mutation_verified": False,
                    "deleted": False,
                    "message": "Provider upload probe returned no file name",
                }
            source = posixpath.join(root, file_name)
            moved = posixpath.join(root, f"{file_name}.moved")
            probe_paths.update({"source": source, "moved": moved})
            content = bytes(info.get("content") or b"")
            expected_size = int(info.get("file_size") or len(content))

            def visible(path: str, expected: bool) -> bool:
                # The probe file is written straight through the Provider's
                # native API, so OpenList's listing cache for ``root`` predates
                # it.  Waiting alone cannot expire that cache; force a refresh
                # before each recheck so the poll converges instead of burning
                # every attempt against a stale directory.
                for attempt in range(12):
                    try:
                        exists = bool(client.exists(path))
                    except Exception:  # noqa: BLE001 - retry eventual visibility
                        exists = not expected
                    if exists is expected:
                        return True
                    time.sleep(0.5)
                    if attempt < 11:
                        refresh_dir(root)
                return False

            deleted = False
            try:
                if not visible(source, True):
                    raise RuntimeError("OpenList did not expose the native probe file")

                def read_verified() -> bytes:
                    last: BaseException | None = None
                    for attempt in range(12):
                        try:
                            if int(client.file_size(source) or 0) != expected_size:
                                raise RuntimeError("OpenList probe file size does not match")
                            if content:
                                data = bytes(client.read_bytes(source))
                                if data != content:
                                    raise RuntimeError(
                                        "OpenList probe file content does not match"
                                    )
                                return data
                            return b""
                        except Exception as exc:  # noqa: BLE001 - retry eventual GET readiness
                            last = exc
                            if attempt < 11:
                                time.sleep(0.5)
                    raise RuntimeError(f"OpenList probe file read failed: {last}") from last

                read_verified()
                client.move(source, moved)
                if not visible(moved, True) or not visible(source, False):
                    raise RuntimeError("OpenList MOVE result could not be verified")
                if int(client.file_size(moved) or 0) != expected_size:
                    raise RuntimeError("OpenList moved probe file size does not match")
                client.delete(moved)
                deleted = visible(moved, False)
                if not deleted:
                    raise RuntimeError("OpenList DELETE result could not be verified")
                return {
                    "organizer_mutation_verified": True,
                    "deleted": True,
                    "message": "OpenList read, MOVE and DELETE checks passed",
                }
            except Exception as exc:  # noqa: BLE001 - return structured probe failure
                # ``deleted`` tells the Provider uploader to skip its own native
                # cleanup, so it may only be true for a DELETE this observer
                # actually issued.  Inferring it from ``not exists()`` conflated
                # "OpenList cannot see the file" with "the file is gone" and
                # leaked one probe file into the cloud drive on every failure —
                # precisely the case where OpenList visibility is what broke.
                for candidate in (source, moved):
                    try:
                        if client.exists(candidate):
                            client.delete(candidate)
                            deleted = True
                    except Exception:  # noqa: BLE001 - Provider cleanup is the fallback
                        pass
                return {
                    "organizer_mutation_verified": False,
                    "deleted": deleted,
                    "message": f"OpenList organizer mutation probe failed: {exc}",
                }

        observe.probe_paths = probe_paths  # type: ignore[attr-defined]
        return observe

    def _location_view(
        self,
        target: Any,
        mounts: dict[str, dict[str, Any]],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            binding = self.targets.resolve_binding(target.destination_id, target.current_revision)
        except Exception:
            binding = None
        mount_id = ""
        resolved_category_paths: dict[str, str] = {}
        for key, category in target.categories.items():
            locator = category.rclone_locator
            if isinstance(locator, RcloneLocator):
                mount_id = mount_id or locator.mount_id
                resolved_category_paths[key] = locator.relative_path
            elif isinstance(locator, LocalLocator):
                resolved_category_paths[key] = locator.relative_path
            if not mount_id and isinstance(category.organizer_locator, OpenListLocator):
                mount_id = category.organizer_locator.mount_id
        mount = mounts.get(mount_id) if mount_id else None
        account_id = binding.account_id if binding else None
        check = self.targets.test_target(target.destination_id)
        remote_ready = True
        connection_ready = True
        webdav_write_required = self._webdav_file_write_required(target.provider_kind)
        provider = self._provider_catalog_item(target.provider_kind)
        relay_upload_capable = bool(
            (provider.get("capabilities") or {}).get("supports_relay_upload")
        )
        native_locator = dict((mount or {}).get("native_locator") or {})
        native_upload_verified, organizer_mutation_verified = (
            self._mount_native_proofs(target.provider_kind, mount)
        )
        if mount is not None:
            remote_ready = bool(
                mount.get("enabled")
                and mount.get("readable")
                and (mount.get("writable") or not webdav_write_required)
                and mount.get("identity_status") in {"verified", "declared"}
                and (not relay_upload_capable or native_upload_verified)
                and (not relay_upload_capable or organizer_mutation_verified)
            )
            connection = next(
                (
                    item
                    for item in self.connections.list_connections()
                    if item.get("connection_id") == mount.get("connection_id")
                ),
                None,
            )
            frozen_connection_revision = int(mount.get("connection_revision") or 0)
            connection_ready = bool(
                connection
                and connection.get("enabled")
                and frozen_connection_revision > 0
                and int(connection.get("revision") or 0) == frozen_connection_revision
            )
            remote_ready = remote_ready and connection_ready
        return {
            "destination_id": target.destination_id,
            "current_revision": int(target.current_revision),
            "name": target.name,
            "kind": "local" if target.target_kind == "local" else (
                "generic" if target.provider_kind == "generic" else "provider"
            ),
            "provider_kind": target.provider_kind,
            "account_id": account_id,
            "account_name": (accounts.get(account_id) or {}).get("name") if account_id else None,
            "mount_id": mount_id or None,
            "connection_id": mount.get("connection_id") if mount else None,
            "identity_status": mount.get("identity_status") if mount else None,
            "identity_confirmation_required": bool(
                mount is not None
                and mount.get("identity_status") not in {"verified", "declared"}
            ),
            "webdav_root": mount.get("webdav_root") if mount else None,
            "webdav_readable": bool((mount or {}).get("readable")),
            "webdav_writable": bool((mount or {}).get("writable")),
            "webdav_file_write_required": webdav_write_required,
            "native_upload_capable": relay_upload_capable,
            "native_upload_verified": native_upload_verified,
            "native_upload_tested_at": native_locator.get("upload_tested_at"),
            "organizer_mutation_verified": organizer_mutation_verified,
            "organizer_tested_at": native_locator.get("organizer_tested_at"),
            "native_only": bool(
                mount is not None
                and mount.get("readable")
                and not mount.get("writable")
                and not webdav_write_required
            ),
            "native_root": ((mount or {}).get("native_locator") or {}).get("value") or "",
            "native_root_name": ((mount or {}).get("native_locator") or {}).get("display_name") or "",
            "resolved_category_paths": resolved_category_paths,
            "enabled": target.enabled,
            "ready": bool(target.enabled and check.get("ok") and remote_ready),
            "status_message": self._location_status_message(
                target_enabled=bool(target.enabled),
                target_valid=bool(check.get("ok")),
                remote_ready=remote_ready,
                mount=mount,
                connection_ready=connection_ready,
                relay_upload_capable=relay_upload_capable,
                native_upload_verified=native_upload_verified,
                organizer_mutation_verified=organizer_mutation_verified,
                webdav_write_required=webdav_write_required,
            ),
        }

    @staticmethod
    def _location_status_message(
        *,
        target_enabled: bool,
        target_valid: bool,
        remote_ready: bool,
        mount: dict[str, Any] | None,
        connection_ready: bool,
        relay_upload_capable: bool,
        native_upload_verified: bool,
        organizer_mutation_verified: bool,
        webdav_write_required: bool,
    ) -> str:
        if target_enabled and target_valid and remote_ready:
            if relay_upload_capable:
                return "Provider 原生直传与 OpenList 整理链路可用"
            if mount is not None and not mount.get("writable") and not webdav_write_required:
                return "可以接收同源原生转存；跨盘 rclone 写入不可用"
            return "可以接收入库任务"
        if mount is not None and mount.get("identity_status") not in {"verified", "declared"}:
            return "需要管理员确认存储身份"
        if mount is not None and not connection_ready:
            return "OpenList 连接已变化，请重新检测目录"
        if relay_upload_capable and not native_upload_verified:
            return "Provider 原生上传尚未通过探测"
        if relay_upload_capable and not organizer_mutation_verified:
            return "OpenList 读取、MOVE 或 DELETE 整理链路尚未通过探测"
        return "需要检查目录或连接"

    def _route_view(self, matrix: dict[str, Any], locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        location_by_id = {item["destination_id"]: item for item in locations}
        result: list[dict[str, Any]] = []
        for row in matrix.get("rows", []):
            values = {
                cell["category_key"]: cell.get("destination_id")
                for cell in row.get("assignments", [])
            }
            assignment_revisions = {
                cell["category_key"]: cell.get("revision")
                for cell in row.get("assignments", [])
            }
            counts: dict[str, int] = {}
            for value in values.values():
                if value:
                    counts[value] = counts.get(value, 0) + 1
            default_id = max(counts, key=counts.get) if counts else None
            overrides = {key: value for key, value in values.items() if value and value != default_id}
            target = location_by_id.get(default_id or "")
            result.append(
                {
                    "search_source_id": row["search_source_id"],
                    "provider_kind": row["provider_kind"],
                    "label": row["label"],
                    "default_destination_id": default_id,
                    "overrides": overrides,
                    "assignment_revisions": assignment_revisions,
                    "ready": all(cell.get("ready") for cell in row.get("assignments", [])),
                    "chain_summary": self._chain_summary(row, target),
                }
            )
        return result

    @staticmethod
    def _chain_summary(source: dict[str, Any], target: dict[str, Any] | None) -> str:
        if target is None:
            return "尚未设置保存位置"
        if source.get("search_source_id") == "sixpan_magnet":
            return "六盘离线后直接入库"
        if source.get("provider_kind") == target.get("provider_kind"):
            return "同源直转，不经过本地"
        if target.get("native_upload_capable"):
            return "来源落地后下载到本地，再由目标云盘原生 API 上传"
        if target.get("kind") == "local":
            return "来源落地后通过 rclone 下载到本地"
        return "来源落地后通过 rclone 搬运到目标网盘"

    # -------------------------------------------------------------- accounts

    def save_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = _text(payload.get("account_id"))
        should_test = not account_id
        if account_id:
            if "name" in payload:
                self.accounts.update_account_name(account_id, payload.get("name"))
            if "config" in payload or "credentials" in payload:
                raise StorageSetupError("账号凭证只能由登录会话或 Provider 自动刷新")
        else:
            provider_kind = _text(payload.get("provider_kind"))
            provider = next(
                (
                    item
                    for item in self.accounts.catalog()
                    if item.get("provider_kind") == provider_kind
                ),
                None,
            )
            if provider and provider.get("auth_methods"):
                raise StorageSetupError("请使用网盘登录授权添加账号，不再支持手工填写 API 或 Token")
            account = self.accounts.create_account(
                provider_kind=provider_kind,
                name=_text(payload.get("name")),
                config=dict(payload.get("config") or {}),
                credentials=dict(payload.get("credentials") or {}),
            )
            account_id = account.account_id

        if should_test:
            test = self.accounts.test_account(account_id)
            if not test.ok:
                raise StorageSetupError(test.message or "账号检测未通过")
        account = self.accounts.get_account(account_id)
        if account is None:
            raise StorageSetupError("账号保存后无法读取")
        current = self.accounts.get_selection(account.provider_kind, "default")
        should_activate = bool(payload.get("activate", True)) or current is None
        if should_activate:
            self.accounts.activate_account(
                "default",
                account_id,
                current.selection_revision if current else None,
            )
        return {"success": True, "account_id": account_id, "message": "账号设置已保存"}

    # --------------------------------------------------------------- openlist

    def save_openlist(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection_id = _text(payload.get("connection_id"))
        existing_connections = self.connections.list_connections()
        if not connection_id:
            if existing_connections:
                raise StorageSetupError(
                    "当前入库链路只支持一个 OpenList WebDAV 连接，请编辑现有连接",
                    code="OPENLIST_CONNECTION_LIMIT",
                    status=409,
                )
            connection_id = "openlist_main"
        elif not any(
            _text(item.get("connection_id")) == connection_id
            for item in existing_connections
        ) and existing_connections:
            raise StorageSetupError(
                "当前入库链路只支持一个 OpenList WebDAV 连接，请编辑现有连接",
                code="OPENLIST_CONNECTION_LIMIT",
                status=409,
            )
        body = {
            "connection_id": connection_id,
            "name": _text(payload.get("name")) or "OpenList WebDAV",
            "base_url": payload.get("base_url"),
            "username": payload.get("username"),
        }
        if payload.get("expected_revision", payload.get("revision")) not in (None, ""):
            body["expected_revision"] = int(
                payload.get("expected_revision", payload.get("revision"))
            )
        if "password" in payload:
            body["password"] = payload.get("password")
        if "enabled" in payload:
            body["enabled"] = payload.get("enabled")
        previous = self.connections.get_connection(connection_id)
        previous_credentials: dict[str, Any] | None = None
        previous_mounts: list[dict[str, Any]] = []
        if previous is not None:
            previous_credentials = self.connections.resolve_credentials(
                connection_id, int(previous.get("revision") or 1)
            )
            previous_mounts = [
                dict(mount)
                for mount in self.mounts.list_mounts()
                if _text(mount.get("connection_id")) == connection_id
            ]
        created = previous is None
        item: dict[str, Any] | None = None
        rclone_state: dict[str, Any] = {}
        try:
            if previous is not None:
                body.pop("connection_id", None)
                item = self.connections.update_connection(connection_id, body)
            else:
                item = self.connections.create_connection(body)
            result = self.connections.test_connection(
                connection_id,
                expected_revision=int((item or {}).get("revision") or 0) or None,
            )
            if not result.get("ok"):
                raise StorageSetupError(
                    result.get("message") or "OpenList WebDAV 检测未通过",
                    code="OPENLIST_PROBE_FAILED",
                    status=503,
                    retryable=True,
                )
            rclone_state = self._sync_rclone_webdav(connection_id)
        except Exception:
            try:
                if created:
                    if self.connections.get_connection(connection_id) is not None:
                        self.connections.delete_connection(connection_id)
                elif previous is not None and previous_credentials is not None and item is not None:
                    current = self.connections.get_connection(connection_id)
                    if current is None:
                        raise StorageSetupError("OpenList 连接在补偿期间消失")
                    restored = self.connections.update_connection(
                        connection_id,
                        {
                            "name": previous.get("name"),
                            "base_url": previous.get("base_url"),
                            "username": previous.get("username"),
                            "password": previous_credentials.get("password"),
                            "enabled": previous.get("enabled", True),
                            "expected_revision": int((item or {}).get("revision") or 0),
                        },
                    )
                    restored_revision = int((restored or {}).get("revision") or 0)
                    for snapshot in previous_mounts:
                        mount_id = _text(snapshot.get("mount_id"))
                        current_mount = self.mounts.get_mount(mount_id)
                        expected_mount_revision = int(snapshot.get("revision") or 0) + 2
                        if (
                            current_mount is None
                            or int(current_mount.get("revision") or 0)
                            != expected_mount_revision
                        ):
                            raise StorageSetupError(
                                "OpenList 回滚期间检测到挂载已被并发修改",
                                code="OPENLIST_COMPENSATION_CONFLICT",
                                status=409,
                            )
                        restored_snapshot = {
                            **snapshot,
                            "connection_revision": restored_revision,
                        }
                        self.mounts.restore_mount_snapshot(
                            mount_id,
                            restored_snapshot,
                            expected_revision=expected_mount_revision,
                        )
            except Exception as compensation_error:
                logger.exception(
                    "event=openlist_save_compensation_failed connection_id=%s created=%s",
                    connection_id,
                    created,
                )
                raise StorageSetupError(
                    "OpenList 保存检测失败，且自动恢复未完成，请刷新后人工检查连接",
                    code="OPENLIST_COMPENSATION_FAILED",
                    status=500,
                    retryable=False,
                ) from compensation_error
            raise
        item = self.connections.get_connection(connection_id)
        if item is None:  # pragma: no cover - guarded by successful test above
            raise StorageSetupError("OpenList 保存后无法读取")
        rclone_available = bool(rclone_state.get("available", True))
        return {
            "success": True,
            "item": item,
            "rclone": rclone_state,
            "rclone_available": rclone_available,
            "message": (
                "OpenList 已保存，WebDAV 与 rclone 连接检测成功"
                if rclone_available
                else _text(rclone_state.get("message"))
                or "OpenList 已保存，但 rclone 不可用，跨盘搬运暂不可用"
            ),
        }

    def delete_openlist(self, connection_id: str) -> dict[str, Any]:
        connection_id = _text(connection_id)
        if not connection_id:
            raise StorageSetupError("connection_id 不能为空", field_errors={"connection_id": "必填"})
        try:
            return self.connections.delete_connection(connection_id)
        except Exception as exc:
            # Keep the aggregate API independent from concrete OpenList error
            # classes while preserving a useful conflict/not-found status.
            message = str(exc)
            lowered = message.lower()
            if "不存在" in message or "not found" in lowered:
                raise StorageSetupError(message, code="OPENLIST_NOT_FOUND", status=404) from exc
            if "挂载" in message or "使用" in message or "引用" in message:
                raise StorageSetupError(message, code="OPENLIST_IN_USE", status=409) from exc
            raise StorageSetupError(message, code="OPENLIST_DELETE_FAILED", status=422) from exc

    # -------------------------------------------------------------- locations

    def save_location(self, payload: dict[str, Any], destination_id: str | None = None) -> dict[str, Any]:
        """Create/revise one administrator-owned location.

        A mount identity is a security decision, not a side effect of a probe.
        The caller must explicitly send ``confirm_identity=true`` whenever a
        new/changed mount is saved (or when an existing unverified mount is
        completed).  Failed target writes compensate an already revised mount
        so the aggregate operation does not leave a half-updated location.
        """

        if payload.get("category_paths") or _text(payload.get("library_root")):
            raise StorageSetupError(
                "分类目录由系统统一生成，保存位置不再接受分类路径配置",
                field_errors={"category_paths": "不允许自定义分类目录"},
            )
        kind = _text(payload.get("kind")) or "provider"
        if kind not in {"provider", "generic", "local"}:
            raise StorageSetupError("保存位置类型不正确", field_errors={"kind": "无效类型"})
        name = _text(payload.get("name"))
        if not name:
            raise StorageSetupError("请填写保存位置名称", field_errors={"name": "必填"})

        existing = self.targets.get_target(destination_id) if destination_id else None
        requested_target_revision = payload.get("expected_revision", payload.get("revision"))
        if existing and requested_target_revision not in (None, ""):
            try:
                requested_target_revision = int(requested_target_revision)
            except (TypeError, ValueError) as exc:
                raise StorageSetupError(
                    "expected_revision 必须是正整数",
                    code="TARGET_CONFLICT",
                    status=409,
                ) from exc
            if requested_target_revision != int(existing.current_revision):
                raise StorageSetupError(
                    "保存位置已被其他管理员修改，请刷新后重试",
                    code="TARGET_CONFLICT",
                    status=409,
                )
        account_id = _text(payload.get("account_id")) or None
        account: Any = None
        if existing and kind == "provider" and not account_id:
            try:
                account_id = self.targets.resolve_binding(
                    existing.destination_id, existing.current_revision
                ).account_id
            except Exception:
                account_id = None
        provider_kind = "local" if kind == "local" else "generic"
        if kind == "provider":
            if not account_id:
                raise StorageSetupError("云盘保存位置必须选择账号", field_errors={"account_id": "必填"})
            account = self.accounts.get_account(account_id)
            if account is None or not account.enabled:
                raise StorageSetupError(
                    "所选云盘账号不存在或已停用",
                    code="ACCOUNT_NOT_FOUND",
                    status=404,
                    field_errors={"account_id": "账号不存在或已停用"},
                )
            provider_kind = account.provider_kind
            requested_provider_kind = _text(payload.get("provider_kind"))
            if requested_provider_kind and requested_provider_kind != provider_kind:
                raise StorageSetupError(
                    "所选网盘类型与账号不一致",
                    code="PROVIDER_MISMATCH",
                    status=409,
                    field_errors={"account_id": "账号类型不匹配"},
                )
        if existing and existing.provider_kind != provider_kind:
            raise StorageSetupError(
                "保存位置类型或云盘账号类型不能直接变更，请新建保存位置",
                code="LOCATION_IMMUTABLE_KIND",
                status=409,
            )

        destination_id = destination_id or _generated_id("dest", name)
        connection_id = _text(payload.get("connection_id"))
        webdav_root = _text(payload.get("webdav_root"))
        native_root = _text(payload.get("native_root"))
        native_root_name = _text(payload.get("native_root_name"))
        explicit_confirm = _bool(payload.get("confirm_identity"), False)
        mount: dict[str, Any] | None = None
        old_mount: dict[str, Any] | None = None
        mount_changed = False
        created_mount_id: str | None = None

        if kind != "local":
            if not connection_id and existing:
                old_mount_id = self._target_mount_id(existing)
                old_mount = self.mounts.get_mount(old_mount_id) if old_mount_id else None
                connection_id = _text((old_mount or {}).get("connection_id"))
            if not connection_id or not self.connections.get_connection(connection_id):
                raise StorageSetupError(
                    "请先连接 OpenList WebDAV",
                    code="OPENLIST_NOT_FOUND",
                    status=404,
                    field_errors={"connection_id": "连接不存在"},
                )
            other_connection_ids = {
                _text(item.get("connection_id"))
                for item in self.mounts.list_mounts()
                if _text(item.get("connection_id"))
                and _text(item.get("connection_id")) != connection_id
                and bool(item.get("enabled", True))
            }
            if other_connection_ids:
                raise StorageSetupError(
                    "同一入库运行时不能混用多个 OpenList 连接，请统一到现有连接",
                    code="MULTIPLE_OPENLIST_CONNECTIONS_UNSUPPORTED",
                    status=409,
                    field_errors={"connection_id": "必须与现有保存位置使用同一连接"},
                )
            if not webdav_root and existing:
                old_mount_id = self._target_mount_id(existing)
                old_mount = old_mount or (self.mounts.get_mount(old_mount_id) if old_mount_id else None)
                webdav_root = _text((old_mount or {}).get("webdav_root"))
            if not webdav_root:
                raise StorageSetupError(
                    "请选择这个保存位置在 OpenList 中的目录",
                    field_errors={"webdav_root": "必填"},
                )
            if kind == "provider" and not native_root:
                if existing:
                    native_root = _text(
                        ((old_mount or {}).get("native_locator") or {}).get("value")
                    )
                if not native_root:
                    raise StorageSetupError(
                        "请填写同源直转使用的云盘保存根目录",
                        field_errors={"native_root": "必填"},
                    )
            mount_id = self._target_mount_id(existing) if existing else None
            old_mount = old_mount or (self.mounts.get_mount(mount_id) if mount_id else None)
            if old_mount and _text(old_mount.get("connection_id")) != connection_id:
                raise StorageSetupError(
                    "已存在保存位置不能更换 OpenList 连接，请新建保存位置",
                    code="LOCATION_CONNECTION_IMMUTABLE",
                    status=409,
                    field_errors={"connection_id": "请新建保存位置"},
                )
            locator_kind = self._locator_kind(provider_kind) if kind == "provider" else "provider_path"
            previous_native_locator = dict((old_mount or {}).get("native_locator") or {})
            storage_identity_id = (
                f"provider:{provider_kind}:account:{account_id}"
                if kind == "provider"
                else f"generic:{destination_id}"
            )
            provisional_native_locator: dict[str, Any] = {}
            if kind == "provider" and native_root:
                provisional_native_locator = {
                    "kind": locator_kind,
                    "value": native_root,
                }
                resolved_display_name = native_root_name or (
                    _text(previous_native_locator.get("display_name"))
                    if _text(previous_native_locator.get("kind")) == locator_kind
                    and _text(previous_native_locator.get("value")) == native_root
                    else ""
                )
                if resolved_display_name:
                    provisional_native_locator["display_name"] = resolved_display_name
            previous_identity_locator = {
                key: previous_native_locator.get(key)
                for key in ("kind", "value", "display_name")
                if previous_native_locator.get(key) not in (None, "")
            }
            identity_inputs_changed = not old_mount or any(
                _text(old_mount.get(key)) != _text(value)
                for key, value in {
                    "name": name,
                    "webdav_root": webdav_root,
                    "provider_kind": provider_kind if kind == "provider" else None,
                    "account_id": account_id,
                    "storage_identity_id": storage_identity_id,
                    "identity_root": webdav_root,
                }.items()
            ) or previous_identity_locator != provisional_native_locator
            identity_status = str((old_mount or {}).get("identity_status") or "unverified")
            if (
                identity_inputs_changed
                or identity_status not in {"verified", "declared"}
            ) and not explicit_confirm:
                raise StorageSetupError(
                    "请确认 OpenList 目录确实对应所选云盘/存储身份",
                    code="IDENTITY_CONFIRMATION_REQUIRED",
                    status=422,
                    field_errors={"confirm_identity": "必须勾选确认"},
                )
            native_locator_payload = (
                self._native_locator_with_probe(
                    provider_kind=provider_kind,
                    account=account,
                    locator_kind=locator_kind,
                    locator_value=native_root,
                    display_name=native_root_name,
                    connection_id=connection_id,
                    webdav_root=webdav_root,
                    previous=previous_native_locator,
                )
                if kind == "provider" and native_root
                else {}
            )
            mount_payload = {
                "connection_id": connection_id,
                "name": name,
                "provider_kind": provider_kind if kind == "provider" else None,
                "account_id": account_id,
                "webdav_root": webdav_root,
                "storage_identity_id": storage_identity_id,
                "identity_root": webdav_root,
                "probe_root": (old_mount or {}).get("probe_root") or "/_fnos_probe",
                "native_locator": native_locator_payload,
            }
            if old_mount:
                # Protect the aggregate edit from a concurrent mount change;
                # the target and mount must be revised from the same snapshot.
                mount_payload["expected_revision"] = int(old_mount.get("revision") or 0)
            mount_changed = not old_mount or any(
                _text(old_mount.get(key)) != _text(mount_payload.get(key))
                for key in ("name", "webdav_root", "provider_kind", "account_id", "storage_identity_id", "identity_root")
            ) or dict(old_mount.get("native_locator") or {}) != dict(mount_payload.get("native_locator") or {})
            if mount_changed:
                try:
                    if mount_id:
                        mount = self.mounts.update_mount(mount_id, mount_payload)
                    else:
                        mount_payload["mount_id"] = _generated_id("mount", name)
                        mount = self.mounts.create_mount(mount_payload)
                        created_mount_id = mount["mount_id"]
                    connection_snapshot = self.connections.get_connection(connection_id)
                    probe = self.mounts.probe_mount(
                        mount["mount_id"],
                        expected_revision=int(mount.get("revision") or 0) or None,
                        expected_connection_revision=int(
                            (connection_snapshot or {}).get("revision") or 0
                        )
                        or None,
                        require_webdav_file_write=self._webdav_file_write_required(
                            provider_kind
                        ),
                    )
                    # probe_mount advances the mount revision even when a
                    # required check fails; carry that revision into the
                    # compensation CAS so we never restore over a concurrent
                    # administrator edit.
                    if probe.get("revision"):
                        mount = {**mount, "revision": probe["revision"]}
                    if probe.get("connection_revision"):
                        mount = {**mount, "connection_revision": probe["connection_revision"]}
                    if (
                        probe.get("ok")
                        and not probe.get("writable")
                        and not probe.get("webdav_file_write_required", True)
                    ):
                        logger.info(
                            "event=storage_mount_native_only mount_id=%s provider=%s "
                            "webdav_readable=%s webdav_writable=%s",
                            mount.get("mount_id"),
                            provider_kind,
                            bool(probe.get("readable")),
                            bool(probe.get("writable")),
                        )
                    if not probe.get("ok"):
                        logger.warning(
                            "event=storage_mount_probe_checks_failed mount_id=%s checks=%s",
                            mount.get("mount_id"),
                            json.dumps(
                                [
                                    {"name": c.get("name"), "ok": c.get("ok"), "message": c.get("message")}
                                    for c in (probe.get("checks") or [])
                                ],
                                ensure_ascii=False,
                            ),
                        )
                        raise StorageSetupError(
                            probe.get("message") or "保存目录读写检测未通过",
                            code="OPENLIST_PROBE_FAILED",
                            status=503,
                            retryable=True,
                        )
                    if explicit_confirm:
                        mount = self.mounts.confirm_identity(
                            mount["mount_id"],
                            expected_revision=int(mount.get("revision") or 0) or None,
                            expected_connection_revision=int(
                                mount.get("connection_revision") or 0
                            )
                            or None,
                        )
                except Exception:
                    self._restore_mount(
                        old_mount,
                        created_mount_id,
                        expected_revision=int((mount or {}).get("revision") or 0) or None,
                    )
                    raise
            else:
                mount = old_mount
                if explicit_confirm and mount and mount.get("identity_status") not in {"verified", "declared"}:
                    mount = self.mounts.confirm_identity(
                        mount["mount_id"],
                        expected_revision=int(mount.get("revision") or 0) or None,
                        expected_connection_revision=int(mount.get("connection_revision") or 0)
                        or None,
                    )

        categories = self._build_categories(
            destination_id=destination_id,
            kind=kind,
            provider_kind=provider_kind,
            connection_id=connection_id,
            mount_id=(mount or {}).get("mount_id"),
            native_root=native_root,
            library_root="",
        )
        try:
            if existing:
                target = self.targets.revise_target(
                    destination_id,
                    name=name,
                    categories=categories,
                    account_id=account_id,
                    binding_locator={"mount_id": (mount or {}).get("mount_id")},
                    expected_revision=int(
                        requested_target_revision or existing.current_revision
                    ),
                )
            else:
                target = self.targets.create_target(
                    destination_id=destination_id,
                    name=name,
                    provider_kind=provider_kind,
                    target_kind="local" if kind == "local" else (
                        "openlist_generic" if kind == "generic" else "provider"
                    ),
                    audience="admin",
                    categories=categories,
                    account_id=account_id,
                    binding_locator={"mount_id": (mount or {}).get("mount_id")},
                )
        except Exception:
            mount_revision = int((mount or {}).get("revision") or 0)
            old_revision = int((old_mount or {}).get("revision") or 0)
            if created_mount_id or mount_revision != old_revision:
                self._restore_mount(
                    old_mount,
                    created_mount_id,
                    expected_revision=mount_revision or None,
                )
            raise
        return {
            "success": True,
            "destination_id": target.destination_id,
            "identity_status": (mount or {}).get("identity_status") if mount else None,
            "message": (
                "保存位置已创建，Provider 原生直传与 OpenList 整理链路已验证"
                if self._supports_relay_upload(provider_kind)
                else "保存位置已创建并通过读写检测"
            ),
        }

    def _restore_mount(
        self,
        old_mount: dict[str, Any] | None,
        created_mount_id: str | None,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Best-effort compensation for a failed aggregate location write."""

        try:
            if created_mount_id:
                self.mounts.delete_mount(created_mount_id)
                return
            if not old_mount:
                return
            mount_id = str(old_mount.get("mount_id") or "")
            if not mount_id:
                return
            if expected_revision is None:
                return
            self.mounts.restore_mount_snapshot(
                mount_id,
                old_mount,
                expected_revision=expected_revision,
            )
        except Exception:
            # The original exception remains the actionable error.  A later
            # preflight will expose a potentially inconsistent mount.  Do not
            # silently swallow the compensation failure: emit a structured
            # event with only object IDs (never credentials or paths).
            logger.exception(
                "event=storage_setup_compensation_failed mount_id=%s created_mount_id=%s",
                str((old_mount or {}).get("mount_id") or ""),
                str(created_mount_id or ""),
            )
            return

    def _build_categories(
        self,
        *,
        destination_id: str,
        kind: str,
        provider_kind: str,
        connection_id: str,
        mount_id: str | None,
        native_root: str,
        library_root: str,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, TargetCategory]:
        categories: dict[str, TargetCategory] = {}
        locator_kind = self._locator_kind(provider_kind) if kind == "provider" else "none"
        for key in CATEGORY_KEYS:
            label = CATEGORY_LABELS.get(key, key)
            # 分类目录是系统规则，不再维护第二套“分类路径”配置。
            relative = _path_join(library_root, label)
            native_value = ""
            if kind == "provider":
                if provider_kind == "tianyi" and locator_kind == "folder_id":
                    # 天翼需要先按根 folder_id 创建/解析分类目录，再把真实 folder_id
                    # 交给同源转存。内部复合定位器不暴露给管理页面。
                    native_value = f"{native_root}::{label}"
                    native_kind = "folder_path"
                else:
                    native_value = _path_join(native_root, label)
                    native_kind = locator_kind
            if kind == "local":
                rclone = LocalLocator(root=f"/storage-targets/{destination_id}", relative_path=relative)
                organizer = None
            else:
                if not mount_id:
                    raise StorageSetupError("保存位置缺少 OpenList mount")
                rclone = RcloneLocator(connection_id=connection_id, mount_id=mount_id, relative_path=relative)
                organizer = OpenListLocator(connection_id=connection_id, mount_id=mount_id, path=relative)
            categories[key] = TargetCategory(
                category_key=key,
                public_label=label,
                native_locator=NativeLocator(native_kind, native_value) if native_value else None,
                rclone_locator=rclone,
                organizer_locator=organizer,
            )
        return categories

    def _locator_kind(self, provider_kind: str) -> str:
        provider = next(
            (item for item in self.accounts.catalog() if item.get("provider_kind") == provider_kind),
            None,
        )
        fields = (
            (provider or {}).get("native_locator_schema")
            or (provider or {}).get("locator_schema")
            or {}
        ).get("fields") or []
        return _text(fields[0].get("name")) if fields else "provider_path"

    @staticmethod
    def _target_mount_id(target: Any) -> str | None:
        if target is None:
            return None
        for category in target.categories.values():
            if isinstance(category.rclone_locator, RcloneLocator):
                return category.rclone_locator.mount_id
            if isinstance(category.organizer_locator, OpenListLocator):
                return category.organizer_locator.mount_id
        return None

    def delete_location(self, destination_id: str) -> dict[str, Any]:
        """Delete a target and its private mount as one database transaction.

        The previous implementation called ``delete_target`` and then
        ``delete_mount`` through two independent connections.  If the mount
        preflight failed after the target commit, the API returned an error
        while leaving a half-deleted location.  The aggregate service owns the
        operation, so it performs all reference checks and child-row deletes
        under one ``BEGIN IMMEDIATE`` transaction.
        """

        target = self.targets.get_target(destination_id)
        if target is None:
            raise StorageSetupError("保存位置不存在")
        mount_id = self._target_mount_id(target)
        with self.connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            target_row = conn.execute(
                "SELECT destination_id FROM storage_targets WHERE destination_id=?",
                (destination_id,),
            ).fetchone()
            if target_row is None:
                raise StorageSetupError("保存位置已被删除，请刷新页面", code="TARGET_NOT_FOUND", status=404)

            if conn.execute(
                "SELECT 1 FROM source_destination_assignments WHERE destination_id=? LIMIT 1",
                (destination_id,),
            ).fetchone():
                raise StorageSetupError(
                    "保存位置仍被入库规则引用，请先移除规则",
                    code="LOCATION_IN_USE",
                    status=409,
                )
            if conn.execute(
                "SELECT 1 FROM import_jobs WHERE destination_id=? LIMIT 1",
                (destination_id,),
            ).fetchone():
                raise StorageSetupError(
                    "保存位置仍被入库任务引用，不能删除",
                    code="LOCATION_IN_USE",
                    status=409,
                )

            if _table_exists(conn, "import_execution_plans"):
                for row in conn.execute("SELECT plan_json FROM import_execution_plans").fetchall():
                    if _json_references(row["plan_json"], {"destination_id", "target_destination_id"}, destination_id):
                        raise StorageSetupError(
                            "保存位置仍被执行计划引用，不能删除",
                            code="LOCATION_IN_USE",
                            status=409,
                        )
            if _table_exists(conn, "transfer_executions"):
                for row in conn.execute(
                    "SELECT snapshot_json, receipt_json, cleanup_json FROM transfer_executions"
                ).fetchall():
                    if any(
                        _json_references(value, {"destination_id", "target_destination_id"}, destination_id)
                        for value in (row["snapshot_json"], row["receipt_json"], row["cleanup_json"])
                    ):
                        raise StorageSetupError(
                            "保存位置仍被运行中的传输执行引用，不能删除",
                            code="LOCATION_IN_USE",
                            status=409,
                        )

            if mount_id:
                mount_row = conn.execute(
                    "SELECT mount_id FROM openlist_mounts WHERE mount_id=?",
                    (mount_id,),
                ).fetchone()
                if mount_row is None:
                    raise StorageSetupError(
                        "保存位置引用的 OpenList 挂载不存在，已拒绝删除以避免扩大不一致",
                        code="LOCATION_INCONSISTENT",
                        status=409,
                    )
                # A mount may be shared by another target revision.  The
                # current target's revisions are deleted below, so exclude
                # this destination from the ownership check.
                for row in conn.execute(
                    "SELECT destination_id, categories_json FROM storage_target_revisions "
                    "WHERE destination_id<>?",
                    (destination_id,),
                ).fetchall():
                    if _json_references(row["categories_json"], {"mount_id"}, mount_id):
                        raise StorageSetupError(
                            "OpenList 挂载仍被其他保存位置引用，不能删除",
                            code="LOCATION_IN_USE",
                            status=409,
                        )
                if _table_exists(conn, "import_execution_plans"):
                    for row in conn.execute("SELECT plan_json FROM import_execution_plans").fetchall():
                        if _json_references(row["plan_json"], {"mount_id"}, mount_id):
                            raise StorageSetupError(
                                "OpenList 挂载仍被执行计划引用，不能删除",
                                code="LOCATION_IN_USE",
                                status=409,
                            )
                if _table_exists(conn, "transfer_executions"):
                    for row in conn.execute(
                        "SELECT snapshot_json, receipt_json, cleanup_json FROM transfer_executions"
                    ).fetchall():
                        if any(
                            _json_references(value, {"mount_id"}, mount_id)
                            for value in (row["snapshot_json"], row["receipt_json"], row["cleanup_json"])
                        ):
                            raise StorageSetupError(
                                "OpenList 挂载仍被传输执行引用，不能删除",
                                code="LOCATION_IN_USE",
                                status=409,
                            )

            binding_rows = conn.execute(
                "SELECT binding_id FROM storage_target_account_bindings WHERE destination_id=?",
                (destination_id,),
            ).fetchall()
            for binding in binding_rows:
                conn.execute(
                    "DELETE FROM storage_target_binding_revisions WHERE binding_id=?",
                    (binding["binding_id"],),
                )
            conn.execute(
                "DELETE FROM storage_target_account_bindings WHERE destination_id=?",
                (destination_id,),
            )
            conn.execute(
                "DELETE FROM storage_target_revisions WHERE destination_id=?",
                (destination_id,),
            )
            cursor = conn.execute(
                "DELETE FROM storage_targets WHERE destination_id=?", (destination_id,)
            )
            if cursor.rowcount != 1:
                raise StorageSetupError("保存位置删除失败", code="LOCATION_DELETE_FAILED", status=409)
            if mount_id:
                conn.execute("DELETE FROM openlist_mount_revisions WHERE mount_id=?", (mount_id,))
                mount_cursor = conn.execute(
                    "DELETE FROM openlist_mounts WHERE mount_id=?", (mount_id,)
                )
                if mount_cursor.rowcount != 1:
                    raise StorageSetupError(
                        "OpenList 挂载删除失败，事务已回滚",
                        code="LOCATION_DELETE_FAILED",
                        status=409,
                    )
        return {"destination_id": destination_id, "mount_id": mount_id, "deleted": True}

    # ---------------------------------------------------------------- routes

    def save_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_routes = payload.get("routes")
        if not isinstance(raw_routes, list):
            raise StorageSetupError("routes 必须是数组")
        if any(not isinstance(item, dict) for item in raw_routes):
            raise StorageSetupError("routes 中的每一项都必须是对象")
        source_ids = [_text(item.get("search_source_id")) for item in raw_routes]
        if len(set(source_ids)) != len(source_ids):
            raise StorageSetupError("同一搜索来源只能提交一条规则", code="ROUTE_CONFLICT", status=409)
        route_by_source = {source_id: item for source_id, item in zip(source_ids, raw_routes)}
        assignments: list[dict[str, Any]] = []
        sources = self.assignments.sources()
        unknown_sources = sorted(set(route_by_source) - set(sources))
        if unknown_sources:
            raise StorageSetupError(
                f"包含未知搜索来源：{', '.join(unknown_sources)}",
                field_errors={"routes": "存在未注册的搜索来源"},
            )
        provider_by_kind = {
            str(item.get("provider_kind") or ""): item
            for item in self.accounts.catalog()
        }
        sixpan = provider_by_kind.get("sixpan") or {}
        if "sixpan_magnet" in sources and not bool(
            (sixpan.get("capabilities") or {}).get("supports_native_offline")
        ):
            raise StorageSetupError(
                "六盘 Provider 未声明 native_offline 能力，不能保存磁链路由",
                code="PROVIDER_CAPABILITY_UNAVAILABLE",
                status=503,
            )
        for source_id in sources:
            route = route_by_source.get(source_id)
            if route is None:
                raise StorageSetupError(f"请设置 {sources[source_id]['label']} 的保存位置")
            default_id = _text(route.get("default_destination_id"))
            if not default_id:
                raise StorageSetupError(f"请设置 {sources[source_id]['label']} 的默认保存位置")
            overrides = dict(route.get("overrides") or {})
            expected_revisions = dict(route.get("assignment_revisions") or {})
            for category_key in CATEGORY_KEYS:
                destination_id = _text(overrides.get(category_key)) or default_id
                target = self.targets.get_target(destination_id)
                if target is None:
                    raise StorageSetupError(
                        f"保存位置不存在：{destination_id}",
                        code="TARGET_NOT_FOUND",
                        status=404,
                    )
                target_view = {
                    "provider_kind": target.provider_kind,
                    "kind": "local" if target.target_kind == "local" else (
                        "generic" if target.target_kind == "openlist_generic" else "provider"
                    ),
                }
                target_mount_id = self._target_mount_id(target)
                target_mount = self.mounts.get_mount(target_mount_id) if target_mount_id else None
                target_view["webdav_writable"] = bool((target_mount or {}).get("writable"))
                (
                    target_view["native_upload_verified"],
                    target_view["organizer_mutation_verified"],
                ) = self._mount_native_proofs(
                    target.provider_kind,
                    target_mount,
                )
                if not self._route_supported(
                    source_id,
                    sources[source_id],
                    target_view,
                    provider_by_kind,
                ):
                    raise StorageSetupError(
                        f"{sources[source_id]['label']} 无法保存到 {target.name}",
                        code="ROUTE_CAPABILITY_UNAVAILABLE",
                        status=422,
                        field_errors={f"routes.{source_id}.{category_key}": "能力矩阵不支持"},
                    )
                assignment = {
                    "search_source_id": source_id,
                    "category_key": category_key,
                    "destination_id": destination_id,
                    "enabled": True,
                }
                expected_revision = expected_revisions.get(category_key)
                if expected_revision not in (None, ""):
                    assignment["expected_revision"] = int(expected_revision)
                assignments.append(assignment)
        saved = self.assignments.replace(assignments)
        return {"success": True, "assignments": [asdict(item) for item in saved], "message": "入库规则已保存"}

    # --------------------------------------------------------------- preflight

    def preflight(self) -> dict[str, Any]:
        # Aggregate preflight is state read-only. Individual account, connection,
        # and mount test actions may persist health, but this overview must not
        # advance revisions or invalidate an administrator's edit CAS token.
        checks: list[dict[str, Any]] = []
        selections = self.accounts.list_active_selections()
        sources = self.assignments.sources()
        matrix = self.assignments.matrix()
        target_ids: set[str] = set()
        source_provider_kinds: set[str] = set()
        target_account_ids: set[str] = set()
        mount_ids: set[str] = set()
        tested_account_ids: set[str] = set()
        dependency_checks: list[dict[str, Any]] = []
        source_mount_keys: set[tuple[str, str]] = set()
        rclone_required = False
        organizer_required = bool(
            self.organizer_enabled()
            if callable(self.organizer_enabled)
            else False
        )

        def add_source_mount(provider_kind: str, account_id: str) -> None:
            key = (provider_kind, account_id)
            if key in source_mount_keys:
                return
            source_mount_keys.add(key)
            try:
                mount = self.mounts.find_mount_for_account(
                    provider_kind,
                    account_id,
                    readable=False,
                )
            except Exception as exc:
                dependency_checks.append({
                    "name": f"来源目录 {provider_kind}",
                    "ok": False,
                    "message": str(exc),
                })
                return
            if mount is not None:
                mount_ids.add(str(mount.get("mount_id") or ""))
                return
            candidates = [
                item
                for item in self.mounts.list_mounts()
                if _text(item.get("provider_kind")) == provider_kind
                and _text(item.get("account_id")) == account_id
                and bool(item.get("enabled", True))
            ]
            if len(candidates) == 1:
                mount_ids.add(_text(candidates[0].get("mount_id")))
                return
            dependency_checks.append({
                "name": f"来源目录 {provider_kind}",
                "ok": False,
                "message": (
                    "跨盘来源缺少可用的唯一 OpenList landing mount"
                    if not candidates
                    else "跨盘来源存在多个 OpenList landing mount，请只保留一个"
                ),
            })

        # Derive dependencies from the active routing matrix.  Unused broken
        # accounts/connections must not block a preflight for configured routes.
        for row in matrix.get("rows", []):
            source_id = str(row.get("search_source_id") or "")
            source = sources.get(source_id, row)
            if source_id:
                source_provider_kinds.add(str(source.get("provider_kind") or ""))
            for cell in row.get("assignments", []):
                destination_id = _text(cell.get("destination_id"))
                if not destination_id:
                    continue
                target_ids.add(destination_id)
                target = self.targets.get_target(destination_id)
                if target is None:
                    continue
                try:
                    binding = self.targets.resolve_binding(destination_id, target.current_revision)
                except Exception:
                    binding = None
                if binding and getattr(binding, "account_id", None):
                    target_account_ids.add(str(binding.account_id))
                category_key = _text(cell.get("category_key"))
                category = target.category(category_key) if category_key else None
                if category is not None:
                    locator = category.rclone_locator
                    if getattr(locator, "mount_id", None):
                        mount_ids.add(str(locator.mount_id))
                    organizer_locator = category.organizer_locator
                    if getattr(organizer_locator, "mount_id", None):
                        mount_ids.add(str(organizer_locator.mount_id))
                source_provider = _text(source.get("provider_kind"))
                source_form = _text(source.get("source_form") or "share").lower()
                target_provider = _text(target.provider_kind)
                cross_provider = bool(
                    source_form != "magnet"
                    and source_provider
                    and target_provider
                    and source_provider != target_provider
                )
                if cross_provider:
                    rclone_required = True
                    selection = selections.get(f"{source_provider}:default")
                    if selection and selection.get("account_id"):
                        add_source_mount(
                            source_provider,
                            _text(selection.get("account_id")),
                        )
                    if category is not None:
                        destination_locator = category.rclone_locator
                        if not isinstance(destination_locator, RcloneLocator):
                            destination_locator = category.organizer_locator
                        if getattr(destination_locator, "mount_id", None):
                            mount_ids.add(str(destination_locator.mount_id))
                if organizer_required and category is not None:
                    organizer_locator = category.organizer_locator
                    if getattr(organizer_locator, "mount_id", None):
                        mount_ids.add(str(organizer_locator.mount_id))

        for source_kind in sorted(kind for kind in source_provider_kinds if kind):
            selection = selections.get(f"{source_kind}:default")
            source_label = next(
                (
                    str(item.get("label") or source_kind)
                    for item in sources.values()
                    if item.get("provider_kind") == source_kind
                ),
                source_kind,
            )
            if selection is None:
                checks.append({"name": f"账号 {source_label}", "ok": False, "message": "尚未设置当前使用账号"})
                continue
            try:
                result = self.accounts.test_account(
                    selection["account_id"], persist_result=False
                )
                tested_account_ids.add(str(selection["account_id"]))
                checks.append({"name": f"账号 {source_label}", "ok": bool(result.ok), "message": result.message})
            except Exception as exc:
                checks.append({"name": f"账号 {source_label}", "ok": False, "message": str(exc)})

        # A target may deliberately bind a non-active account revision, but it
        # still must be enabled and healthy before it can receive a task.
        for account_id in sorted(target_account_ids):
            if account_id in tested_account_ids:
                continue
            account = self.accounts.get_account(account_id)
            if account is None:
                checks.append({"name": f"目标账号 {account_id}", "ok": False, "message": "账号不存在"})
                continue
            if not account.enabled:
                checks.append({"name": f"目标账号 {account.name}", "ok": False, "message": "账号已停用"})
                continue
            try:
                result = self.accounts.test_account(account_id, persist_result=False)
                checks.append({"name": f"目标账号 {account.name}", "ok": bool(result.ok), "message": result.message})
            except Exception as exc:
                checks.append({"name": f"目标账号 {account.name}", "ok": False, "message": str(exc)})

        checks.extend(dependency_checks)

        connections = {item["connection_id"]: item for item in self.connections.list_connections()}
        required_connection_ids = {
            str((self.mounts.get_mount(mid) or {}).get("connection_id") or "")
            for mid in mount_ids
        } - {""}
        for connection_id in sorted(required_connection_ids):
            connection = connections.get(connection_id)
            if connection is None:
                checks.append({"name": f"OpenList {connection_id}", "ok": False, "message": "连接不存在"})
                continue
            try:
                result = self.connections.test_connection(
                    connection_id,
                    expected_revision=int(connection.get("revision") or 0) or None,
                    persist_result=False,
                )
                checks.append({"name": f"OpenList {connection['name']}", "ok": bool(result.get("ok")), "message": result.get("message")})
            except Exception as exc:
                checks.append({"name": f"OpenList {connection['name']}", "ok": False, "message": str(exc)})

        if rclone_required:
            if len(required_connection_ids) != 1:
                checks.append({
                    "name": "rclone OpenList Remote",
                    "ok": False,
                    "message": (
                        "跨盘路由缺少可解析的 OpenList 连接"
                        if not required_connection_ids
                        else "当前运行时只支持一个 OpenList WebDAV 连接，路由中检测到多个连接"
                    ),
                })
            else:
                connection_id = next(iter(required_connection_ids))
                try:
                    result = self._probe_rclone_webdav(connection_id)
                    checks.append({
                        "name": "rclone OpenList Remote",
                        "ok": True,
                        "message": _text(result.get("message")) or "rclone WebDAV 连接成功",
                    })
                except Exception as exc:
                    checks.append({
                        "name": "rclone OpenList Remote",
                        "ok": False,
                        "message": str(exc),
                    })

        for mount_id in sorted(mount_ids):
            mount = self.mounts.get_mount(mount_id)
            if mount is None:
                checks.append({"name": f"目录 {mount_id}", "ok": False, "message": "挂载不存在"})
                continue
            try:
                connection = connections.get(str(mount.get("connection_id") or ""))
                result = self.mounts.probe_mount(
                    mount_id,
                    expected_revision=int(mount.get("revision") or 0) or None,
                    expected_connection_revision=int((connection or {}).get("revision") or 0)
                    or None,
                    persist_result=False,
                    require_webdav_file_write=self._webdav_file_write_required(
                        _text(mount.get("provider_kind"))
                    ),
                )
                identity_ok = mount.get("identity_status") in {"verified", "declared"}
                provider_kind = _text(mount.get("provider_kind"))
                relay_upload_capable = self._supports_relay_upload(provider_kind)
                native_upload_ok, organizer_mutation_ok = self._mount_native_proofs(
                    provider_kind,
                    mount,
                )
                mount_ok = bool(result.get("ok")) and identity_ok
                if relay_upload_capable:
                    mount_ok = bool(
                        mount_ok
                        and native_upload_ok
                        and organizer_mutation_ok
                    )
                if not identity_ok:
                    mount_message = "目录检测通过，但尚未确认存储身份"
                elif relay_upload_capable and not native_upload_ok:
                    mount_message = "Provider 原生上传探测证明缺失，请重新保存并检测该位置"
                elif relay_upload_capable and not organizer_mutation_ok:
                    mount_message = "OpenList 读取、MOVE 或 DELETE 整理探测证明缺失"
                else:
                    mount_message = _text(result.get("message"))
                checks.append({
                    "name": f"目录 {mount['name']}",
                    "ok": mount_ok,
                    "message": mount_message,
                })
            except Exception as exc:
                checks.append({"name": f"目录 {mount['name']}", "ok": False, "message": str(exc)})

        for destination_id in sorted(target_ids):
            target = self.targets.get_target(destination_id)
            if target is None:
                checks.append({"name": f"保存位置 {destination_id}", "ok": False, "message": "目标不存在"})
                continue
            result = self.targets.test_target(destination_id)
            checks.append({
                "name": f"保存位置 {target.name}",
                "ok": bool(result.get("ok")),
                "message": "结构正确" if result.get("ok") else "；".join(result.get("errors") or []),
            })

        routes_ok = bool(matrix.get("rows")) and all(
            cell.get("ready")
            for row in matrix.get("rows", [])
            for cell in row.get("assignments", [])
        )
        checks.append({
            "name": "来源保存规则",
            "ok": routes_ok,
            "message": "所有搜索来源和分类均已配置" if routes_ok else "仍有来源或分类未配置",
        })
        ok = bool(checks) and all(item["ok"] for item in checks)
        return {"success": True, "ok": ok, "checks": checks, "message": "全部检查通过，可以开始入库" if ok else "部分检查未通过"}


def _table_exists(connection: Any, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (str(table_name),),
    ).fetchone()
    return row is not None


def _json_references(raw: Any, keys: set[str], wanted: str) -> bool:
    """Search a structured plan/receipt without substring false positives."""

    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    expected = str(wanted or "")
    normalized_keys = {str(item) for item in keys}

    def contains(candidate: Any) -> bool:
        if isinstance(candidate, dict):
            for key, item in candidate.items():
                key_text = str(key or "")
                if key_text in normalized_keys and str(item or "") == expected:
                    return True
                if contains(item):
                    return True
            return False
        if isinstance(candidate, list):
            return any(contains(item) for item in candidate)
        return False

    return bool(expected and contains(value))


__all__ = ["StorageSetupError", "StorageSetupService"]
