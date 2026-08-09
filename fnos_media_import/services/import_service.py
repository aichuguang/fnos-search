"""V1 import application service.

Provider submission is exclusively handled by the durable transfer graph.
This service owns job creation/retry orchestration and manual media-library
refresh only; it has no provider-specific submit callbacks or route logic.
"""

from __future__ import annotations

from typing import Any, Callable

from ..classifiers.link_classifier import detect_link
from ..fnos_paths import (
    match_actual_dirs,
    match_library,
    normalize_library_name,
    normalize_remote_hint,
    path_tails,
    split_values,
)
from .import_job_service import ImportJobCreationService, ImportJobRetryService


class ImportService:
    FNOS_DIR_REQUIRED_MESSAGE = (
        "未匹配到飞牛媒体库真实刷新目录，已跳过扫描；"
        "请先在媒体库管理中确认分类 dir_list"
    )

    def __init__(
        self,
        *,
        database: Any,
        config: Any,
        fnos: Any,
        source_resolver: Any,
        destination_resolver: Any,
        plan_import: Callable[..., dict[str, Any]],
        validate_plan: Callable[..., Any],
        plan_validation_deps: Any,
        execution_repository: Any,
        enqueue_transfer: Callable[[int], Any],
        enqueue_transfer_with_connection: Callable[[Any, int], Any],
        source_record_service: Any,
        config_revision_provider: Callable[[], int] | None = None,
        executor_id: str = "web",
    ) -> None:
        self.db = database
        self.config = config
        self.fnos = fnos
        self.job_creation = ImportJobCreationService(
            database=database,
            config=config,
            source_resolver=source_resolver,
            destination_resolver=destination_resolver,
            plan_import=plan_import,
            validate_plan=validate_plan,
            plan_validation_deps=plan_validation_deps,
            execution_repository=execution_repository,
            enqueue_transfer=enqueue_transfer,
            enqueue_transfer_with_connection=enqueue_transfer_with_connection,
            source_record_service=source_record_service,
            config_revision_provider=config_revision_provider,
            executor_id=executor_id,
        )
        self.job_retry = ImportJobRetryService(
            database=database,
            config=config,
            validate_plan=validate_plan,
            plan_validation_deps=plan_validation_deps,
            execution_repository=execution_repository,
            enqueue_transfer=enqueue_transfer,
        )

    def detect(self, url: str, password: str = "") -> dict[str, Any]:
        return detect_link(url, {}, password=password).to_dict()

    def create_import_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.job_creation.create(payload)

    def create_internal_import_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.job_creation.create_internal(payload)

    def retry_job(self, job_id: int) -> dict[str, Any]:
        return self.job_retry.retry(job_id)

    def get_job_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self.db.job_queries.get_by_idempotency_key(idempotency_key)

    def refresh_media(self, category_key: str) -> dict[str, Any]:
        category = self.config.category(category_key)
        library_info = self._fnos_library_info(category)
        library = library_info["library"]
        dir_list = library_info["dir_list"]
        if not dir_list:
            return {
                "success": False,
                "message": self.FNOS_DIR_REQUIRED_MESSAGE,
                "library": library,
                "dir_list": [],
                "target_hints": library_info.get("target_hints") or [],
                "skipped": True,
            }
        try:
            guid = str(library_info.get("guid") or "")
            if guid:
                return self.fnos.refresh_guid(
                    guid,
                    library=library,
                    dir_list=dir_list,
                )
            return self.fnos.refresh(library, dir_list=dir_list)
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"飞牛媒体库刷新异常：{exc}",
                "library": library,
                "dir_list": dir_list,
            }

    def _fnos_library_info(
        self,
        category: dict[str, Any],
        target_path: str = "",
        extra_hints: list[str] | None = None,
    ) -> dict[str, Any]:
        library = str(category.get("fnos_lib") or category.get("label") or "").strip()
        fallback_dirs = self._fnos_dir_list(
            category,
            target_path=target_path,
        )
        target_hints = self._fnos_target_hints(
            category,
            target_path=target_path,
            extra_hints=extra_hints,
        )
        try:
            result = self.fnos.refresh_libraries()
        except Exception:  # noqa: BLE001
            result = {}
        items = (
            result.get("items")
            if isinstance(result, dict) and isinstance(result.get("items"), list)
            else []
        )
        matched = self._match_fnos_library(items, library)
        if matched:
            actual_dirs = self._match_fnos_target_dirs(
                matched.get("dir_list"), target_hints
            )
            return {
                "library": str(matched.get("name") or matched.get("title") or library),
                "guid": str(matched.get("guid") or ""),
                "dir_list": actual_dirs or fallback_dirs,
                "target_hints": target_hints,
            }
        return {
            "library": library,
            "guid": "",
            "dir_list": fallback_dirs,
            "target_hints": target_hints,
        }

    @classmethod
    def _match_fnos_library(
        cls, items: list[Any], library: str
    ) -> dict[str, Any] | None:
        return match_library(items, library)

    @staticmethod
    def _normalize_fnos_library_name(value: Any) -> str:
        return normalize_library_name(value)

    @classmethod
    def _fnos_target_hints(
        cls,
        category: dict[str, Any],
        target_path: str = "",
        extra_hints: list[str] | None = None,
    ) -> list[str]:
        values = [
            *(extra_hints or []),
            category.get("fnos_dir_list"),
            target_path,
            category.get("label"),
        ]
        result: list[str] = []
        for value in values:
            for item in cls._split_fnos_values(value):
                normalized = cls._normalize_remote_hint(item)
                if normalized and normalized not in result:
                    result.append(normalized)
        return result

    @classmethod
    def _match_fnos_target_dirs(
        cls, actual_value: Any, target_hints: list[str]
    ) -> list[str]:
        actual_dirs = cls._absolute_fnos_dirs(actual_value)
        if not actual_dirs or not target_hints:
            return []
        for hint in target_hints:
            matches = cls._match_actual_fnos_dirs(actual_dirs, hint)
            if matches and (
                cls._hint_segment_count(hint) >= 2
                or len(matches) == 1
                or hint.startswith("/")
            ):
                return matches
        return []

    @classmethod
    def _match_actual_fnos_dirs(
        cls, actual_dirs: list[str], hint: str
    ) -> list[str]:
        return match_actual_dirs(actual_dirs, hint)

    @staticmethod
    def _path_tails(path: str) -> list[str]:
        return path_tails(path)

    @classmethod
    def _hint_segment_count(cls, value: str) -> int:
        return len(
            [
                part
                for part in cls._normalize_remote_hint(value).strip("/").split("/")
                if part
            ]
        )

    @staticmethod
    def _normalize_remote_hint(value: Any) -> str:
        return normalize_remote_hint(value)

    @staticmethod
    def _split_fnos_values(value: Any) -> list[str]:
        return split_values(value)

    @staticmethod
    def _fnos_dir_list(
        category: dict[str, Any],
        target_path: str = "",
    ) -> list[str]:
        configured = category.get("fnos_dir_list")
        if isinstance(configured, str):
            values: list[Any] = configured.replace("\n", ",").replace("|", ",").split(",")
        elif isinstance(configured, (list, tuple, set)):
            values = list(configured)
        else:
            values = []
        target = str(target_path or "").strip()
        if target.startswith("/"):
            values = [target]
        return ImportService._absolute_fnos_dirs(values)

    @staticmethod
    def _absolute_fnos_dirs(value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            values = value.replace("\n", ",").replace("|", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            values = [value]
        result: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if not text.startswith("/"):
                continue
            normalized = ImportService._normalize_remote_hint(text)
            if normalized and normalized not in result:
                result.append(normalized)
        return result


__all__ = ["ImportService"]
