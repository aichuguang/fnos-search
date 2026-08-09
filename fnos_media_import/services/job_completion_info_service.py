from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..constants import JOB_PROVIDER_SUBMITTING, JOB_WAITING_TRANSFER
from ..planning import is_data_plane_strategy
from ..planning.job_plan import plan_path, plan_strategy, validated_v1_execution_plan_from_job


class JobCompletionInfoService:
    """Builds a stable completion view from job, Organizer and rclone state."""

    def __init__(
        self,
        *,
        common_virtual_path: Callable[[list[str]], str],
        path_health_checks: Callable[..., list[dict[str, Any]]],
        plan_loader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.common_virtual_path = common_virtual_path
        self.path_health_checks = path_health_checks
        self.plan_loader = plan_loader or validated_v1_execution_plan_from_job

    def build(self, job: dict[str, Any]) -> dict[str, Any]:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        # V1 jobs carry their immutable paths in execution_plan.  Resolve this
        # branch before any historical category/staging fallback so a mutable
        # config edit can never change the status view or organizer root.
        try:
            execution_plan = self.plan_loader(job)
        except Exception as exc:  # noqa: BLE001
            # Historical staging/v3 jobs are diagnostic-only after the V1 cutover.
            # Never rebuild paths from mutable category/config fields.
            message = f"completion view requires a valid V1 execution_plan: {exc}"
            return {
                "source_provider_kind": str(job.get("source_provider_kind") or ""),
                "source_form": str(job.get("source_form") or ""),
                "search_source_id": "",
                "transfer_strategy": "",
                "destination_id": str(job.get("destination_id") or ""),
                "destination_provider_kind": "",
                "official_save_path": "",
                "rclone_target_path": "",
                "openlist_visible_path": "",
                "organizer_scan_path": "",
                "organized_target_path": "",
                "completion_stage": "review",
                "completion_checks": self._preserved_checks(completion),
                "manual_review": True,
                "plan_error": message,
            }
        if execution_plan:
            strategy = plan_strategy(execution_plan)
            source = (
                execution_plan.get("source")
                if isinstance(execution_plan.get("source"), dict)
                else {}
            )
            routing = (
                execution_plan.get("routing")
                if isinstance(execution_plan.get("routing"), dict)
                else {}
            )
            destination = (
                execution_plan.get("destination")
                if isinstance(execution_plan.get("destination"), dict)
                else {}
            )
            final_ref = plan_path(execution_plan, "destination_final")
            staging_ref = plan_path(execution_plan, "destination_staging")
            landing_ref = plan_path(execution_plan, "source_landing")
            organizer_ref = plan_path(execution_plan, "organizer")
            official_save_path = str(
                completion.get("official_save_path")
                or (landing_ref if is_data_plane_strategy(strategy) else final_ref).get("path")
                or job.get("target_path")
                or ""
            ).strip()
            rclone_target_path = str(staging_ref.get("path") or "").strip()
            openlist_path = str(
                completion.get("openlist_visible_path")
                or organizer_ref.get("path")
                or staging_ref.get("path")
                or ""
            ).strip()
            organizer_path = str(completion.get("organizer_scan_path") or organizer_ref.get("path") or openlist_path).strip()
            organized_path = self._organized_target_path(job, completion)
            stage = str(completion.get("stage") or job.get("status") or "").strip()
            data_plane = is_data_plane_strategy(strategy)
            rclone_pending = data_plane and stage in {
                JOB_PROVIDER_SUBMITTING,
                "waiting_transfer",
                "transferring",
                "waiting_openlist",
                JOB_WAITING_TRANSFER,
            }
            checks = self._preserved_checks(completion)
            health_checks = self.path_health_checks(
                official_save_path=official_save_path,
                official_save_label="来源落地路径" if data_plane else "官方网盘保存路径",
                rclone_target_path=rclone_target_path,
                rclone_target_required=bool(staging_ref),
                openlist_visible_path=openlist_path,
                organizer_scan_path=organizer_path,
                organized_target_path=organized_path,
                openlist_required=not rclone_pending,
                organizer_required=not rclone_pending,
            )
            return {
                "source_provider_kind": str(source.get("provider_kind") or ""),
                "source_form": str(source.get("source_form") or ""),
                "search_source_id": str(
                    source.get("search_source_id")
                    or routing.get("search_source_id")
                    or ""
                ),
                "transfer_strategy": strategy,
                "destination_id": str(destination.get("id") or job.get("destination_id") or ""),
                "destination_provider_kind": str(destination.get("provider_kind") or ""),
                "official_save_path": official_save_path,
                "rclone_target_path": rclone_target_path,
                "openlist_visible_path": openlist_path,
                "organizer_scan_path": organizer_path,
                "organized_target_path": organized_path,
                "completion_stage": stage,
                "completion_checks": [*checks, *health_checks],
            }
        # Unreachable: validated_v1_execution_plan_from_job either returned a plan or
        # the method returned the manual-review view above.
        return {
            "source_provider_kind": str(job.get("source_provider_kind") or ""),
            "source_form": str(job.get("source_form") or ""),
            "search_source_id": "",
            "transfer_strategy": "",
            "destination_id": str(job.get("destination_id") or ""),
            "destination_provider_kind": "",
            "official_save_path": "",
            "rclone_target_path": "",
            "openlist_visible_path": "",
            "organizer_scan_path": "",
            "organized_target_path": "",
            "completion_stage": "review",
            "completion_checks": self._preserved_checks(completion),
            "manual_review": True,
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
