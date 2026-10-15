from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from ..config_persistence import (
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    advanced_config_payload_mode,
    advanced_config_subset,
    persisted_config_from_settings,
)
from ..time_utils import utc_now_iso
from ..repositories.update_repository import HistoryCleanupBlockedError


logger = logging.getLogger(__name__)


class _AdvancedConfigSuperseded(RuntimeError):
    """Raised inside an atomic updater when a newer config must be preserved."""


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
        return {"success": True, "summary": self.deps.db.update.history_cleanup_summary()}, 200

    def cleanup_history(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "清理参数必须是 JSON 对象"}, 400
        if not self.deps.payload_bool(payload, "confirm", False):
            return {"success": False, "message": "请先确认清理历史记录"}, 400
        try:
            result = self.deps.db.update.cleanup_history_records(
                backup=self.deps.payload_bool(payload, "backup", False),
                vacuum=self.deps.payload_bool(payload, "vacuum", True),
                backup_prefix="app_before_history_cleanup",
            )
        except HistoryCleanupBlockedError as exc:
            return {
                "success": False, "code": "HISTORY_CLEANUP_BUSY", "message": str(exc), "blockers": exc.blockers,
            }, 409
        cleared_total = int(result.get("cleared_total") or 0)
        message = f"历史记录清理完成，已删除 {cleared_total} 条历史记录；定时追更订阅与来源已保留。"
        if result.get("backup", {}).get("path"):
            message += f" 备份：{result['backup']['path']}"
        if result.get("vacuum_error"):
            message += f"；空间整理失败但数据已清理：{result['vacuum_error']}"
        return {"success": True, "message": message, "result": result, "summary": result.get("after")}, 200

    def advanced(self) -> tuple[dict[str, Any], int]:
        try:
            body = self.deps.advanced_response(self.deps.raw_config(), self.deps.db.app_settings.get_all())
        except ValueError as exc:
            logger.error("advanced config is not a valid V1 stored document: %s", exc)
            return {
                "success": False,
                "code": "ADVANCED_CONFIG_MIGRATION_REQUIRED",
                "message": str(exc),
            }, 409
        return {"success": True, **body}, 200

    def export_advanced(self) -> tuple[dict[str, Any], int]:
        """Build a versioned, sensitive backup document for an authenticated admin."""

        settings = self.deps.db.app_settings.get_all()
        try:
            stored = persisted_config_from_settings(settings)
        except ValueError as exc:
            logger.error("refusing to export invalid advanced config: %s", exc)
            return {
                "success": False,
                "code": "ADVANCED_CONFIG_MIGRATION_REQUIRED",
                "message": str(exc),
            }, 409
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

            message = (
                "高级配置已覆盖导入到数据库，并已重载运行时服务"
                if mode == "replace"
                else "高级配置已合并保存到数据库，并已重载运行时服务"
            )
            return {
                "success": True,
                "message": message,
                "mode": mode,
                "reload": reload_result,
                **self.deps.advanced_response(self.deps.raw_config(), self.deps.db.app_settings.get_all()),
            }, 200

    def _persist_advanced_config(self, payload: dict[str, Any]) -> tuple[bool, Any, dict[str, Any]]:
        def updater(previous: Any, _existed: bool) -> dict[str, Any]:
            current = previous if isinstance(previous, dict) else {}
            return self.deps.normalize_advanced(payload, current_stored=current)

        existed, previous, normalized = self.deps.db.app_settings.update_atomic(
            self.deps.advanced_key,
            updater,
        )
        return bool(existed), copy.deepcopy(previous), normalized

    def _rollback_advanced_config(
        self,
        *,
        expected: dict[str, Any],
        previous: Any,
        previous_exists: bool,
    ) -> dict[str, Any]:
        superseded = False
        rolled_back = False
        try:
            rolled_back = bool(
                self.deps.db.app_settings.compare_and_set(
                    self.deps.advanced_key,
                    expected,
                    previous,
                    expected_exists=True,
                    replacement_exists=previous_exists,
                )
            )
            if not rolled_back:
                # Provider tokens live in immutable account revisions, not
                # in advanced_config. Any mismatch therefore represents a
                # genuine newer writer and must not be merged or masked.
                superseded = True
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
        self.deps.db.app_settings.set_many(updates)
        return {"success": True, "message": "系统设置已保存", "settings": self.deps.effective_settings()}, 200

    def update_all(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "设置格式不正确"}, 400

        basic_payload = payload.get("settings") or {}
        search_payload = payload.get("search") or {}
        advanced_payload = payload.get("advanced")
        if advanced_payload is None:
            # A basic-settings-only transaction still has to pass the V1
            # normalizer; do not synthesize the removed bare patch shape.
            advanced_payload = {
                "config": {
                    "format": ADVANCED_CONFIG_EXPORT_FORMAT,
                    "version": ADVANCED_CONFIG_EXPORT_VERSION,
                    "stored": {},
                },
                "mode": "merge",
                "scope": "stored",
            }
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

                previous, _current = self.deps.db.app_settings.mutate_all_atomic(persist)
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
            **self.deps.advanced_response(self.deps.raw_config(), self.deps.db.app_settings.get_all()),
        }
        return response, 200

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
        try:
            def restore(current: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
                for key, expected_value in expected.items():
                    if key not in current or current.get(key) != expected_value:
                        raise _AdvancedConfigSuperseded()

                replacements: dict[str, Any] = {}
                delete_keys: set[str] = set()
                for key in expected:
                    if key in previous:
                        replacements[key] = copy.deepcopy(previous[key])
                    else:
                        delete_keys.add(key)
                return replacements, delete_keys

            self.deps.db.app_settings.mutate_all_atomic(restore)
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
