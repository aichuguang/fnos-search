from __future__ import annotations

import copy
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..config_persistence import (
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    advanced_config_payload_mode,
    advanced_config_subset,
    sanitize_advanced_config,
)
from ..constants import ROUTE_CLOUD139_DIRECT
from ..storage.registry import UnknownStorageDriver, get_driver, registered_driver_kinds
from ..storage.types import StorageTarget, TargetCategoryConfig
from ..time_utils import utc_now_iso


logger = logging.getLogger(__name__)


_RUNTIME_MANAGED_SIXPAN_TOKEN_FIELDS = ("access_token", "refresh_token")


class _AdvancedConfigSuperseded(RuntimeError):
    """Raised inside an atomic updater when a newer config must be preserved."""


def _without_runtime_managed_sixpan_tokens(value: Any) -> Any:
    """Return a comparison copy that ignores only runtime-refreshed tokens."""

    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    sixpan = result.get("sixpan")
    if isinstance(sixpan, dict):
        for field in _RUNTIME_MANAGED_SIXPAN_TOKEN_FIELDS:
            sixpan.pop(field, None)
        if not sixpan:
            result.pop("sixpan", None)
    return result


def _restore_previous_config_with_token_deltas(
    *,
    previous: Any,
    expected: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Roll back config fields while retaining tokens refreshed after the write.

    Only token values that differ from the failed write are carried forward.
    This prevents a token supplied by the failed payload itself from escaping
    the rollback while still preserving a token refresh performed during
    ``reload_runtime``.
    """

    restored = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    expected_sixpan = expected.get("sixpan") if isinstance(expected.get("sixpan"), dict) else {}
    current_sixpan = current.get("sixpan") if isinstance(current.get("sixpan"), dict) else {}
    missing = object()
    for field in _RUNTIME_MANAGED_SIXPAN_TOKEN_FIELDS:
        expected_value = expected_sixpan.get(field, missing)
        current_value = current_sixpan.get(field, missing)
        if current_value == expected_value:
            continue
        restored_sixpan = restored.get("sixpan")
        if not isinstance(restored_sixpan, dict):
            restored_sixpan = {}
            restored["sixpan"] = restored_sixpan
        if current_value is missing:
            restored_sixpan.pop(field, None)
            if not restored_sixpan:
                restored.pop("sixpan", None)
        else:
            restored_sixpan[field] = copy.deepcopy(current_value)
    return restored


def _storage_bool(value: Any, default: bool = False) -> bool:
    """Parse a YAML/JSON/environment storage boolean without trusting truthiness."""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}


def _target_mode_label(target_cfg: dict[str, Any]) -> str:
    """面向用户的传输方式标签：如实描述，不把跨盘搬运描述成秒转。"""

    driver = str(target_cfg.get("driver") or "").strip().lower()
    native = str(target_cfg.get("native_ingress") or "").strip().lower()
    if driver == "cmcc_api":
        return "原生转存" if native == ROUTE_CLOUD139_DIRECT else "云盘上传"
    if driver == "rclone":
        return "跨盘搬运" if str(target_cfg.get("remote") or "").strip() else "本地入库"
    return "通用目标"


def _build_storage_target(destination_id: str, target_cfg: dict[str, Any]) -> StorageTarget:
    """Materialize a StorageTarget from a raw ``storage.targets.<id>`` dict."""

    categories: dict[str, TargetCategoryConfig] = {}
    raw_categories = target_cfg.get("categories") if isinstance(target_cfg.get("categories"), dict) else {}
    for key, cfg in raw_categories.items():
        if not isinstance(cfg, dict):
            continue
        categories[key] = TargetCategoryConfig(
            final_root=str(cfg.get("final_root") or ""),
            organizer_namespace_id=str(cfg.get("organizer_namespace_id") or ""),
            organizer_root=str(cfg.get("organizer_root") or ""),
        )
    config = {key: value for key, value in target_cfg.items() if key != "categories"}
    return StorageTarget(
        destination_id=destination_id,
        name=str(target_cfg.get("name") or destination_id),
        driver=str(target_cfg.get("driver") or ""),
        enabled=_storage_bool(target_cfg.get("enabled"), True),
        guest_selectable=_storage_bool(target_cfg.get("guest_selectable"), False),
        config=config,
        categories=categories,
    )


def _storage_targets(raw_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    storage = raw_config.get("storage") if isinstance(raw_config.get("storage"), dict) else {}
    targets = storage.get("targets") if isinstance(storage.get("targets"), dict) else {}
    return {key: value for key, value in targets.items() if isinstance(value, dict)}


@dataclass(frozen=True)
class SettingsDependencies:
    db: Any
    raw_config: Callable[[], dict[str, Any]]
    redact_config: Callable[[dict[str, Any]], dict[str, Any]]
    advanced_response: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    normalize_advanced: Callable[..., dict[str, Any]]
    advanced_key: str
    reload_runtime: Callable[[], dict[str, Any]]
    effective_settings: Callable[[], dict[str, Any]]
    payload_bool: Callable[[dict[str, Any], str, bool], bool]
    search_providers: Callable[[], list[dict[str, Any]]] = lambda: []
    storage_probe: Callable[[StorageTarget], dict[str, Any]] | None = None


class SettingsService:
    def __init__(self, dependencies: SettingsDependencies) -> None:
        self.deps = dependencies
        # SQLite coordinates different processes; this lock also prevents two
        # requests in the same process from reloading and rolling back runtime
        # state concurrently.
        self._advanced_update_lock = threading.Lock()

    def config(self) -> tuple[dict[str, Any], int]:
        return {"success": True, "config": self.deps.redact_config(self.deps.raw_config())}, 200

    def history_summary(self) -> tuple[dict[str, Any], int]:
        return {"success": True, "summary": self.deps.db.history_cleanup_summary()}, 200

    def cleanup_history(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not self.deps.payload_bool(payload, "confirm", False):
            return {"success": False, "message": "请先确认清理历史记录"}, 400
        result = self.deps.db.cleanup_history_records(backup=self.deps.payload_bool(payload, "backup", True), vacuum=self.deps.payload_bool(payload, "vacuum", True), backup_prefix="app_before_history_cleanup")
        cleared_total = int(result.get("cleared_total") or 0)
        message = f"历史记录清理完成，已删除 {cleared_total} 条历史记录；定时追更订阅与来源已保留。"
        if result.get("backup", {}).get("path"):
            message += f" 备份：{result['backup']['path']}"
        if result.get("vacuum_error"):
            message += f"；空间整理失败但数据已清理：{result['vacuum_error']}"
        return {"success": True, "message": message, "result": result, "summary": result.get("after")}, 200

    def advanced(self) -> tuple[dict[str, Any], int]:
        return {"success": True, **self.deps.advanced_response(self.deps.raw_config(), self.deps.db.get_app_settings())}, 200

    def export_advanced(self) -> tuple[dict[str, Any], int]:
        """Build a versioned, sensitive backup document for an authenticated admin."""

        settings = self.deps.db.get_app_settings()
        raw_stored = settings.get(self.deps.advanced_key) if isinstance(settings.get(self.deps.advanced_key), dict) else {}
        stored = sanitize_advanced_config(raw_stored, current={}, preserve_secret_placeholders=False)
        document = {
            "format": ADVANCED_CONFIG_EXPORT_FORMAT,
            "version": ADVANCED_CONFIG_EXPORT_VERSION,
            "exported_at": utc_now_iso(),
            "source": {
                "application": "fnos-media-import",
                "config_key": self.deps.advanced_key,
                "stored": "数据库中的高级配置覆盖项；重新导入时默认使用此范围。",
                "effective": "当前运行时有效高级配置；包含 YAML、环境变量与数据库覆盖合并后的结果。",
                "sensitive": True,
            },
            "stored": copy.deepcopy(stored),
            "effective": advanced_config_subset(self.deps.raw_config()),
        }
        return {
            "success": True,
            "document": document,
            "meta": {
                "stored_sections": sorted(stored.keys()),
                "effective_sections": sorted(document["effective"].keys()),
                "contains_secrets": True,
            },
        }, 200

    def update_advanced(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        mode = advanced_config_payload_mode(payload)
        with self._advanced_update_lock:
            try:
                previous_exists, previous, normalized = self._persist_advanced_config(payload)
            except ValueError as exc:
                return {"success": False, "message": str(exc)}, 400

            try:
                reload_result = self.deps.reload_runtime()
            except Exception as exc:  # noqa: BLE001
                logger.exception("advanced config runtime reload failed; attempting database rollback")
                rollback = self._rollback_advanced_config(
                    expected=normalized,
                    previous=previous,
                    previous_exists=previous_exists,
                )
                return {
                    "success": False,
                    "message": f"{rollback['message']}；{exc}",
                    "mode": mode,
                    "rolled_back": rollback["rolled_back"],
                    "superseded": rollback["superseded"],
                    "runtime_restored": rollback["runtime_restored"],
                }, int(getattr(exc, "status_code", 500) or 500)

            warnings: list[str] = []
            config_payload = payload.get("config") if isinstance(payload, dict) else None
            if str(payload.get("source") or "").strip().lower() == "import" and isinstance(config_payload, dict):
                if "format" not in config_payload and "version" not in config_payload:
                    warnings.append("已按旧版无版本配置文件导入；建议重新导出一份带版本信息的备份。")
            message = (
                "高级配置已覆盖导入到数据库，并已重载运行时服务"
                if mode == "replace"
                else "高级配置已合并保存到数据库，并已重载运行时服务"
            )
            return {
                "success": True,
                "message": message,
                "mode": mode,
                "warnings": warnings,
                "reload": reload_result,
                **self.deps.advanced_response(self.deps.raw_config(), self.deps.db.get_app_settings()),
            }, 200

    def _persist_advanced_config(self, payload: dict[str, Any]) -> tuple[bool, Any, dict[str, Any]]:
        atomic_update = getattr(self.deps.db, "update_app_setting_atomic", None)
        if callable(atomic_update):
            def updater(previous: Any, _existed: bool) -> dict[str, Any]:
                current = previous if isinstance(previous, dict) else {}
                return self.deps.normalize_advanced(payload, current_stored=current)

            existed, previous, normalized = atomic_update(self.deps.advanced_key, updater)
            return bool(existed), copy.deepcopy(previous), normalized

        # Compatibility fallback for lightweight test doubles and older DB
        # adapters.  The process-local lock still prevents in-process lost
        # updates, while the production Database uses the atomic path above.
        settings = self.deps.db.get_app_settings()
        existed = self.deps.advanced_key in settings
        previous = copy.deepcopy(settings.get(self.deps.advanced_key))
        current = previous if isinstance(previous, dict) else {}
        normalized = self.deps.normalize_advanced(payload, current_stored=current)
        self.deps.db.set_app_settings({self.deps.advanced_key: normalized})
        return existed, previous, normalized

    def _rollback_advanced_config(
        self,
        *,
        expected: dict[str, Any],
        previous: Any,
        previous_exists: bool,
    ) -> dict[str, Any]:
        compare_and_set = getattr(self.deps.db, "compare_and_set_app_setting", None)
        superseded = False
        rolled_back = False
        try:
            if callable(compare_and_set):
                rolled_back = bool(
                    compare_and_set(
                        self.deps.advanced_key,
                        expected,
                        previous,
                        expected_exists=True,
                        replacement_exists=previous_exists,
                    )
                )
                if not rolled_back:
                    # Runtime reload can legitimately refresh SixPan tokens in
                    # the same JSON document before failing.  An exact CAS then
                    # looks superseded even though every user-managed config
                    # field still equals this failed write.  Re-check and merge
                    # that narrow token delta under the repository write lock.
                    atomic_update = getattr(self.deps.db, "update_app_setting_atomic", None)
                    if callable(atomic_update):

                        def rollback_preserving_tokens(current: Any, existed: bool) -> dict[str, Any]:
                            if (
                                not existed
                                or not isinstance(current, dict)
                                or _without_runtime_managed_sixpan_tokens(current)
                                != _without_runtime_managed_sixpan_tokens(expected)
                            ):
                                raise _AdvancedConfigSuperseded()
                            return _restore_previous_config_with_token_deltas(
                                previous=previous,
                                expected=expected,
                                current=current,
                            )

                        try:
                            atomic_update(self.deps.advanced_key, rollback_preserving_tokens)
                            rolled_back = True
                        except _AdvancedConfigSuperseded:
                            superseded = True
                    else:
                        superseded = True
            else:
                # This path is only used by process-local compatibility doubles,
                # so no concurrent writer can pass the enclosing lock.
                self.deps.db.set_app_settings(
                    {self.deps.advanced_key: previous if previous_exists else {}}
                )
                rolled_back = True
        except Exception:  # noqa: BLE001
            logger.exception("advanced config database rollback failed")
            return {
                "message": "高级配置重载失败，数据库回滚也失败；请检查服务日志并重新保存配置。",
                "rolled_back": False,
                "superseded": False,
                "runtime_restored": False,
            }

        runtime_restored = False
        try:
            # Reconcile this process even when another process superseded the
            # failed write; reload always reads the current database value.
            self.deps.reload_runtime()
            runtime_restored = True
        except Exception:  # noqa: BLE001
            logger.exception("advanced config runtime restoration failed")
            runtime_restored = False

        if superseded:
            message = "高级配置重载失败；检测到其他进程已保存更新，未覆盖较新的数据库配置。"
        elif runtime_restored:
            message = "高级配置重载失败，数据库改动已回滚，运行时已恢复。"
        else:
            message = "高级配置重载失败，数据库改动已回滚，但运行时恢复失败；请检查服务日志。"
        return {
            "message": message,
            "rolled_back": rolled_back,
            "superseded": superseded,
            "runtime_restored": runtime_restored,
        }

    def settings(self) -> tuple[dict[str, Any], int]:
        return {"success": True, "settings": self.deps.effective_settings()}, 200

    def update_settings(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            updates = self._normalize_basic_settings(payload)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}, 400
        if not updates:
            return {"success": False, "message": "没有可保存的设置"}, 400
        self.deps.db.set_app_settings(updates)
        return {"success": True, "message": "系统设置已保存", "settings": self.deps.effective_settings()}, 200

    def update_all(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "设置格式不正确"}, 400

        basic_payload = payload.get("settings") or {}
        search_payload = payload.get("search") or {}
        advanced_payload = payload.get("advanced") or {"config": {}}
        if not isinstance(basic_payload, dict) or not isinstance(search_payload, dict) or not isinstance(advanced_payload, dict):
            return {"success": False, "message": "设置格式不正确"}, 400

        written: dict[str, Any] = {}
        previous: dict[str, Any] = {}
        advanced_changed = False

        with self._advanced_update_lock:
            try:
                def persist(current: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
                    nonlocal written, advanced_changed
                    updates = self._normalize_basic_settings(basic_payload)
                    updates["search.providers"] = self._normalize_search_providers(
                        search_payload,
                        current.get("search.providers"),
                    )
                    current_advanced = current.get(self.deps.advanced_key)
                    current_advanced = current_advanced if isinstance(current_advanced, dict) else {}
                    normalized_advanced = self.deps.normalize_advanced(
                        advanced_payload,
                        current_stored=current_advanced,
                    )
                    advanced_changed = normalized_advanced != current_advanced
                    if advanced_changed:
                        updates[self.deps.advanced_key] = normalized_advanced
                    written = copy.deepcopy(updates)
                    return updates, set()

                atomic_mutate = getattr(self.deps.db, "mutate_app_settings_atomic", None)
                if not callable(atomic_mutate):
                    raise RuntimeError("数据库不支持统一配置事务")
                previous, _current = atomic_mutate(persist)
            except ValueError as exc:
                return {"success": False, "message": str(exc)}, 400
            except Exception as exc:  # noqa: BLE001
                logger.exception("unified settings persistence failed")
                return {"success": False, "message": f"设置保存失败：{exc}"}, 500

            reload_result: dict[str, Any] = {}
            if advanced_changed:
                try:
                    reload_result = self.deps.reload_runtime()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("unified settings runtime reload failed; attempting rollback")
                    rollback = self._rollback_all_settings(expected=written, previous=previous)
                    return {
                        "success": False,
                        "message": f"{rollback['message']}；{exc}",
                        "rolled_back": rollback["rolled_back"],
                        "superseded": rollback["superseded"],
                        "runtime_restored": rollback["runtime_restored"],
                    }, int(getattr(exc, "status_code", 500) or 500)

        response = {
            "success": True,
            "message": "系统设置已全部保存",
            "settings": self.deps.effective_settings(),
            "search_providers": self.deps.search_providers(),
            "reload": reload_result,
            **self.deps.advanced_response(self.deps.raw_config(), self.deps.db.get_app_settings()),
        }
        return response, 200

    def storage_status(self) -> tuple[dict[str, Any], int]:
        """列出存储目标，只返回白名单字段和驱动能力，绝不返回任何连接凭证。"""

        raw = self.deps.raw_config()
        storage = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}
        if not _storage_bool(storage.get("enabled"), False):
            return {"success": True, "enabled": False, "default_target": "", "targets": []}, 200
        targets = _storage_targets(raw)
        default_target = str(storage.get("default_target") or "").strip()
        items: list[dict[str, Any]] = []
        for destination_id, target_cfg in targets.items():
            driver_kind = str(target_cfg.get("driver") or "").strip().lower()
            capabilities: dict[str, Any] = {}
            driver_error = ""
            if driver_kind:
                try:
                    driver = get_driver(driver_kind)
                    target = _build_storage_target(destination_id, target_cfg)
                    validation_errors = driver.validate(target)
                    capabilities = asdict(driver.capabilities(target))
                    if validation_errors:
                        driver_error = "；".join(validation_errors)
                except UnknownStorageDriver:
                    driver_error = f"未知存储驱动：{driver_kind}"
                except Exception as exc:  # noqa: BLE001
                    driver_error = str(exc)
            items.append(
                {
                    "destination_id": destination_id,
                    "name": str(target_cfg.get("name") or destination_id),
                    "driver": driver_kind,
                    "enabled": _storage_bool(target_cfg.get("enabled"), True),
                    "guest_selectable": _storage_bool(target_cfg.get("guest_selectable"), False),
                    "default": destination_id == default_target,
                    "mode_label": _target_mode_label(target_cfg),
                    "capabilities": capabilities,
                    **({"driver_error": driver_error} if driver_error else {}),
                }
            )
        items.sort(key=lambda item: (not item["enabled"], not item["default"], item["destination_id"]))
        return {
            "success": True,
            "enabled": True,
            "default_target": default_target,
            "targets": items,
        }, 200

    def storage_test(self, destination_id: Any) -> tuple[dict[str, Any], int]:
        """校验目标配置，并在运行时已接入探测器时执行真实连接检测。"""

        destination_id_text = str(destination_id or "").strip()
        if not destination_id_text:
            return {"success": False, "message": "缺少 destination_id"}, 400
        target_cfg = _storage_targets(self.deps.raw_config()).get(destination_id_text)
        if not isinstance(target_cfg, dict):
            return {"success": False, "message": f"存储目标不存在：{destination_id_text}"}, 404
        driver_kind = str(target_cfg.get("driver") or "").strip().lower()
        target = _build_storage_target(destination_id_text, target_cfg)
        try:
            driver = get_driver(driver_kind)
        except UnknownStorageDriver:
            errors = [f"未知存储驱动：{driver_kind}"]
            return {
                "success": False,
                "ok": False,
                "destination_id": destination_id_text,
                "errors": errors,
                "message": "目标配置检查未通过",
            }, 400
        errors = driver.validate(target)
        if not errors and callable(self.deps.storage_probe):
            try:
                probe = self.deps.storage_probe(target)
            except Exception as exc:  # noqa: BLE001
                probe = {
                    "success": False,
                    "ok": False,
                    "destination_id": destination_id_text,
                    "checks": [],
                    "errors": [str(exc)],
                    "message": "目标连接检测异常",
                }
            ok = bool(probe.get("ok"))
            return {
                **probe,
                "success": ok,
                "ok": ok,
                "destination_id": destination_id_text,
                "errors": list(probe.get("errors") or []),
            }, (200 if ok else 400)
        ok = not errors
        return {
            "success": ok,
            "ok": ok,
            "destination_id": destination_id_text,
            "errors": errors,
            "message": "目标配置检查通过" if ok else "目标配置检查未通过",
        }, (200 if ok else 400)

    def storage_update(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """最小更新：修改已有目标的 enabled / guest_selectable，复用高级配置持久化路径。"""

        if not isinstance(payload, dict):
            return {"success": False, "message": "请求格式不正确"}, 400
        destination_id = str(payload.get("destination_id") or "").strip()
        if not destination_id:
            return {"success": False, "message": "缺少 destination_id"}, 400
        target_cfg = _storage_targets(self.deps.raw_config()).get(destination_id)
        if not isinstance(target_cfg, dict):
            return {"success": False, "message": f"存储目标不存在：{destination_id}"}, 404
        if "enabled" not in payload and "guest_selectable" not in payload:
            return {"success": False, "message": "没有可保存的修改"}, 400

        settings = self.deps.db.get_app_settings()
        current_stored = settings.get(self.deps.advanced_key) if isinstance(settings.get(self.deps.advanced_key), dict) else {}
        merged_storage = copy.deepcopy(current_stored.get("storage"))
        if not isinstance(merged_storage, dict):
            merged_storage = copy.deepcopy(self.deps.raw_config().get("storage"))
        merged_storage = merged_storage if isinstance(merged_storage, dict) else {}
        targets = merged_storage.setdefault("targets", {})
        if not isinstance(targets, dict):
            targets = {}
            merged_storage["targets"] = targets
        target_patch = targets.setdefault(destination_id, {})
        if not isinstance(target_patch, dict):
            target_patch = {}
            targets[destination_id] = target_patch
        if "enabled" in payload:
            target_patch["enabled"] = _storage_bool(payload.get("enabled"), _storage_bool(target_patch.get("enabled"), True))
        if "guest_selectable" in payload:
            target_patch["guest_selectable"] = _storage_bool(
                payload.get("guest_selectable"),
                _storage_bool(target_patch.get("guest_selectable"), False),
            )

        result, status_code = self.update_advanced({"config": {"storage": merged_storage}})
        if status_code != 200:
            return result, status_code
        return {
            "success": True,
            "message": f"存储目标 {destination_id} 已更新",
            "destination_id": destination_id,
        }, 200

    def public_storage_targets(self) -> tuple[dict[str, Any], int]:
        """访客可选的存储目标提示：enabled + guest_selectable + 驱动已注册。"""

        raw = self.deps.raw_config()
        storage = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}
        if not _storage_bool(storage.get("enabled"), False):
            return {"success": True, "targets": []}, 200
        targets = _storage_targets(raw)
        default_target = str(storage.get("default_target") or "").strip()
        known_drivers = registered_driver_kinds()
        items: list[dict[str, Any]] = []
        for destination_id, target_cfg in targets.items():
            if not _storage_bool(target_cfg.get("enabled"), True):
                continue
            if not _storage_bool(target_cfg.get("guest_selectable"), False):
                continue
            driver_kind = str(target_cfg.get("driver") or "").strip().lower()
            if driver_kind not in known_drivers:
                continue
            try:
                driver = get_driver(driver_kind)
                target = _build_storage_target(destination_id, target_cfg)
                if driver.validate(target):
                    continue
            except Exception:  # noqa: BLE001
                continue
            items.append(
                {
                    "destination_id": destination_id,
                    "name": str(target_cfg.get("name") or destination_id),
                    "default": destination_id == default_target,
                    "mode_label": _target_mode_label(target_cfg),
                }
            )
        items.sort(key=lambda item: (not item["default"], item["destination_id"]))
        return {"success": True, "targets": items}, 200

    def _normalize_basic_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        public_payload, submission_payload = payload.get("public") or {}, payload.get("submission") or {}
        if not isinstance(public_payload, dict) or not isinstance(submission_payload, dict):
            raise ValueError("设置格式不正确")
        updates: dict[str, Any] = {}
        for key in ("allow_anonymous_search", "request_query_enabled", "hide_full_links"):
            if key in public_payload:
                updates[f"public.{key}"] = self.deps.payload_bool(public_payload, key, True)
        if "mode" in submission_payload:
            mode = str(submission_payload.get("mode") or "").strip().lower()
            if mode not in {"auto", "review", "mixed"}:
                raise ValueError("处理模式只能是 auto / review / mixed")
            updates["submission.mode"] = mode
        return updates

    def _normalize_search_providers(self, payload: dict[str, Any], current: Any) -> dict[str, Any]:
        provider_payload = payload.get("providers", payload.get("items", {}))
        if isinstance(provider_payload, list):
            iterable = provider_payload
        elif isinstance(provider_payload, dict):
            iterable = [
                {"key": key, **value}
                for key, value in provider_payload.items()
                if isinstance(value, dict)
            ]
        else:
            raise ValueError("搜索源设置格式不正确")

        known = {
            str(item.get("key") or "").strip()
            for item in self.deps.search_providers()
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        }
        provider_settings = copy.deepcopy(current) if isinstance(current, dict) else {}
        for item in iterable:
            if not isinstance(item, dict):
                raise ValueError("搜索源设置格式不正确")
            key = str(item.get("key") or "").strip()
            if key not in known:
                raise ValueError(f"未知搜索源：{key}")
            existing = provider_settings.get(key) if isinstance(provider_settings.get(key), dict) else {}
            try:
                priority = int(item.get("priority", existing.get("priority", 100)))
            except (TypeError, ValueError):
                priority = 100
            provider_settings[key] = {
                "enabled": self.deps.payload_bool(item, "enabled", bool(existing.get("enabled", True))),
                "priority": max(1, min(999, priority)),
            }
        return provider_settings

    def _rollback_all_settings(
        self,
        *,
        expected: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        atomic_mutate = getattr(self.deps.db, "mutate_app_settings_atomic", None)
        if not callable(atomic_mutate):
            return {
                "message": "设置重载失败，数据库不支持事务回滚；请检查服务日志。",
                "rolled_back": False,
                "superseded": False,
                "runtime_restored": False,
            }

        try:
            def restore(current: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
                for key, expected_value in expected.items():
                    if key == self.deps.advanced_key:
                        current_value = current.get(key)
                        if not isinstance(current_value, dict) or (
                            _without_runtime_managed_sixpan_tokens(current_value)
                            != _without_runtime_managed_sixpan_tokens(expected_value)
                        ):
                            raise _AdvancedConfigSuperseded()
                    elif key not in current or current.get(key) != expected_value:
                        raise _AdvancedConfigSuperseded()

                replacements: dict[str, Any] = {}
                delete_keys: set[str] = set()
                for key, expected_value in expected.items():
                    if key == self.deps.advanced_key:
                        restored = _restore_previous_config_with_token_deltas(
                            previous=previous.get(key),
                            expected=expected_value,
                            current=current.get(key) if isinstance(current.get(key), dict) else {},
                        )
                        if restored:
                            replacements[key] = restored
                        elif key in previous:
                            replacements[key] = previous[key]
                        else:
                            delete_keys.add(key)
                    elif key in previous:
                        replacements[key] = copy.deepcopy(previous[key])
                    else:
                        delete_keys.add(key)
                return replacements, delete_keys

            atomic_mutate(restore)
            rolled_back = True
            superseded = False
        except _AdvancedConfigSuperseded:
            rolled_back = False
            superseded = True
        except Exception:  # noqa: BLE001
            logger.exception("unified settings database rollback failed")
            return {
                "message": "设置重载失败，数据库回滚也失败；请检查服务日志并重新保存。",
                "rolled_back": False,
                "superseded": False,
                "runtime_restored": False,
            }

        runtime_restored = False
        try:
            self.deps.reload_runtime()
            runtime_restored = True
        except Exception:  # noqa: BLE001
            logger.exception("unified settings runtime restoration failed")

        if superseded:
            message = "设置重载失败；检测到其他进程已保存更新，未覆盖较新的配置。"
        elif runtime_restored:
            message = "设置重载失败，本次全部改动已回滚，运行时已恢复。"
        else:
            message = "设置重载失败，本次全部改动已回滚，但运行时恢复失败；请检查服务日志。"
        return {
            "message": message,
            "rolled_back": rolled_back,
            "superseded": superseded,
            "runtime_restored": runtime_restored,
        }
