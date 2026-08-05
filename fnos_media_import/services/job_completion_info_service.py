from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..constants import JOB_PROVIDER_SUBMITTING, JOB_WAITING_TRANSFER
from ..content_guard import BT_SOURCE_TYPES


class JobCompletionInfoService:
    """Builds a stable completion view from job, Organizer and rclone state."""

    def __init__(
        self,
        *,
        category: Callable[[str], dict[str, Any]],
        uses_rclone_staging: Callable[[dict[str, Any]], bool],
        is_staging_path: Callable[[str, dict[str, Any]], bool],
        common_virtual_path: Callable[[list[str]], str],
        rclone_completed_root: Callable[..., str],
        cloud139_plan: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]],
        sixpan_plan: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        map_category_path: Callable[[str, dict[str, Any]], str],
        path_health_checks: Callable[..., list[dict[str, Any]]],
    ) -> None:
        self.category = category
        self.uses_rclone_staging = uses_rclone_staging
        self.is_staging_path = is_staging_path
        self.common_virtual_path = common_virtual_path
        self.rclone_completed_root = rclone_completed_root
        self.cloud139_plan = cloud139_plan
        self.sixpan_plan = sixpan_plan
        self.map_category_path = map_category_path
        self.path_health_checks = path_health_checks

    def build(self, job: dict[str, Any]) -> dict[str, Any]:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        category_key = str(job.get("category") or "").strip()
        category = self.category(category_key)
        source_type = str(job.get("source_type") or "").strip().lower()
        directory_plan = raw_data.get("directory_plan") if isinstance(raw_data.get("directory_plan"), dict) else {}
        staging_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        official_save_path = str(
            completion.get("official_save_path")
            or directory_plan.get("target_path")
            or job.get("target_path")
            or ""
        ).strip()
        uses_staging = self.uses_rclone_staging(job)
        rclone_target_path = (
            str(
                staging_plan.get("storage_job_root")
                or staging_plan.get("storage_staging_category_root")
                or category.get("mobile_target_path")
                or ""
            ).strip()
            if uses_staging
            else ""
        )
        openlist_path = str(completion.get("openlist_visible_path") or "").strip()
        organizer_path = str(completion.get("organizer_scan_path") or "").strip()
        organized_path = self._organized_target_path(job, completion)

        if uses_staging:
            openlist_path = self._without_staging_path(openlist_path, category)
            organizer_path = self._without_staging_path(organizer_path, category)

        latest_task = job.get("latest_organizer_task") if isinstance(job.get("latest_organizer_task"), dict) else {}
        latest_root = str(latest_task.get("openlist_root_path") or "").strip()
        if latest_root and not self.is_staging_path(latest_root, category):
            openlist_path = openlist_path or latest_root
            organizer_path = organizer_path or latest_root

        if uses_staging and (not openlist_path or not organizer_path):
            rclone_root = self._root_from_rclone_events(job, category_key, category)
            openlist_path = openlist_path or rclone_root
            organizer_path = organizer_path or rclone_root

        if not openlist_path:
            openlist_path = self._fallback_openlist_path(
                job, source_type, category, directory_plan, official_save_path
            )
            organizer_path = organizer_path or openlist_path

        if uses_staging:
            openlist_path = self._without_staging_path(openlist_path, category)
            organizer_path = self._without_staging_path(organizer_path, category)

        stage = str(completion.get("stage") or job.get("status") or "").strip()
        rclone_pending = uses_staging and stage in {
            JOB_PROVIDER_SUBMITTING,
            "waiting_transfer",
            "transferring",
            "waiting_openlist",
            JOB_WAITING_TRANSFER,
        }
        checks = self._preserved_checks(completion)
        health_checks = self.path_health_checks(
            official_save_path=official_save_path,
            official_save_label="中转保存路径" if uses_staging else "官方网盘保存路径",
            rclone_target_path=rclone_target_path,
            rclone_target_required=uses_staging,
            openlist_visible_path=openlist_path,
            organizer_scan_path=organizer_path,
            organized_target_path=organized_path,
            openlist_required=not rclone_pending,
            organizer_required=not rclone_pending,
        )
        return {
            "official_save_path": official_save_path,
            "rclone_target_path": rclone_target_path,
            "openlist_visible_path": openlist_path,
            "organizer_scan_path": organizer_path,
            "organized_target_path": organized_path,
            "completion_stage": stage,
            "completion_checks": [*checks, *health_checks],
        }

    def _organized_target_path(self, job: dict[str, Any], completion: dict[str, Any]) -> str:
        stored = str(completion.get("organized_target_path") or "").strip()
        if stored:
            return stored
        mappings = job.get("organizer_mappings") if isinstance(job.get("organizer_mappings"), list) else []
        ready_targets = [
            str(item.get("target_path") or "").strip()
            for item in mappings
            if isinstance(item, dict)
            and str(item.get("status") or "") == "ready"
            and str(item.get("target_path") or "").strip()
        ]
        parents = [str(Path(path.replace("\\", "/")).parent).replace("\\", "/") for path in ready_targets]
        return self.common_virtual_path(parents) if parents else ""

    def _root_from_rclone_events(
        self,
        job: dict[str, Any],
        category_key: str,
        category: dict[str, Any],
    ) -> str:
        events = job.get("rclone_file_events") if isinstance(job.get("rclone_file_events"), list) else []
        targets = [
            str(event.get("target_path") or "").strip()
            for event in events
            if isinstance(event, dict) and str(event.get("target_path") or "").strip()
        ]
        if not category or not targets:
            return ""
        return self.rclone_completed_root(category_key, category, {"target_paths": targets, "job": job})

    def _fallback_openlist_path(
        self,
        job: dict[str, Any],
        source_type: str,
        category: dict[str, Any],
        directory_plan: dict[str, Any],
        official_save_path: str,
    ) -> str:
        if not category:
            return ""
        if source_type == "cloud139":
            return str(self.cloud139_plan(job, category, directory_plan).get("root_path") or "").strip()
        if source_type in BT_SOURCE_TYPES:
            return str(self.sixpan_plan(job, category).get("root_path") or "").strip()
        return self.map_category_path(official_save_path, category) if official_save_path else ""

    def _without_staging_path(self, path: str, category: dict[str, Any]) -> str:
        return "" if path and self.is_staging_path(path, category) else path

    @staticmethod
    def _preserved_checks(completion: dict[str, Any]) -> list[dict[str, Any]]:
        stored = completion.get("checks") if isinstance(completion.get("checks"), list) else []
        generated_names = {
            "official_save_path",
            "rclone_target_path",
            "openlist_visible_path",
            "organizer_scan_path",
            "organized_target_path",
            "organizer_enabled",
            "openlist_configured",
        }
        return [
            check
            for check in stored
            if not (isinstance(check, dict) and str(check.get("name") or "") in generated_names)
        ]
