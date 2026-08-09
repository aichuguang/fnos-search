from __future__ import annotations

import unittest
from inspect import signature
from pathlib import Path

from fnos_media_import.classifiers.link_classifier import detect_link
from fnos_media_import.path_planning import map_execution_path_to_organizer
from fnos_media_import.providers.btbtla import BtbtlaClient
from fnos_media_import.providers.pansou import PanSouClient
from fnos_media_import.services.btbtla_proxy_diagnostics_service import (
    BtbtlaProxyDiagnosticsService,
)
from fnos_media_import.services.public_bt_resolve_service import PublicBtResolveService
from fnos_media_import.services.public_manual_preview_service import (
    PublicManualPreviewService,
)
from fnos_media_import.services.public_resource_detail_service import (
    PublicResourceDetailService,
)
from fnos_media_import.services.public_resource_service import PublicResourceDependencies
from fnos_media_import.services.public_sixpan_preview_service import (
    PublicSixpanPreviewService,
)
from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationService,
)
from fnos_media_import.updater.discovery import UpdateDiscovery


ROOT = Path(__file__).resolve().parents[1]


class RetiredFacadeCutoverTests(unittest.TestCase):
    def test_production_tree_has_no_retired_execution_symbols(self) -> None:
        python_sources = []
        for path in (ROOT / "fnos_media_import").rglob("*.py"):
            if path.name == "v1_cutover.py":
                continue
            python_sources.append(path.read_text(encoding="utf-8"))
        production = "\n".join(python_sources)
        for retired in (
            "target_route",
            "quark_to_mobile",
            "cloud139_direct",
            "cloud189_direct",
            "sixpan_offline",
            "LegacyNativeAdapter",
            "DatabaseDomainMixin",
            "DefaultAdapterRegistry",
            "ActiveQuarkImporter",
            "_wire_v1_runtime_build",
            "evaluate_bt_content_risk",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, production)

        verifier_source = (
            ROOT / "fnos_media_import" / "transfer" / "verifier.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("def __call__", verifier_source)

        jobs_script = (ROOT / "static" / "admin-jobs.js").read_text(encoding="utf-8")
        self.assertNotIn("relay_upload_mobile", jobs_script)
        self.assertIn("Provider 原生 API 直传", jobs_script)

    def test_link_classification_has_no_retired_route_input(self) -> None:
        callables = (
            detect_link,
            PanSouClient,
            BtbtlaClient,
            BtbtlaProxyDiagnosticsService,
            UpdateDiscovery,
            PublicManualPreviewService,
            PublicSixpanPreviewService,
            PublicSubmissionPreparationService,
            PublicBtResolveService,
            PublicResourceDependencies,
            PublicResourceDetailService.build,
        )
        for callable_object in callables:
            parameters = set(signature(callable_object).parameters)
            with self.subTest(callable=getattr(callable_object, "__name__", repr(callable_object))):
                self.assertNotIn("route", parameters)
                self.assertNotIn("routes", parameters)
                self.assertNotIn("current_routes", parameters)
                self.assertNotIn("validate_destination", parameters)

        app_source = (ROOT / "fnos_media_import" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("routes=lambda: {}", app_source)
        self.assertNotIn("routes={},", app_source)
        self.assertNotIn("current_routes=", app_source)
        self.assertNotIn("validate_destination=", app_source)

    def test_v1_lifecycle_calls_do_not_probe_legacy_signatures(self) -> None:
        package = ROOT / "fnos_media_import"
        for relative in (
            "providers/adapters.py",
            "transfer/provider_relay_executor.py",
            "transfer/mobile_relay_executor.py",
            "transfer/orchestrator.py",
            "services/job_cancellation_service.py",
        ):
            with self.subTest(relative=relative):
                source = (package / relative).read_text(encoding="utf-8")
                self.assertNotIn("inspect.signature", source)
                self.assertNotIn("_call_supported", source)

    def test_planner_and_staging_facades_are_deleted(self) -> None:
        self.assertFalse(
            (ROOT / "fnos_media_import" / "services" / "import_planner.py").exists()
        )
        self.assertFalse(
            (
                ROOT
                / "fnos_media_import"
                / "services"
                / "import_staging_service.py"
            ).exists()
        )
        source = (ROOT / "fnos_media_import" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("services.import_planner", source)
        self.assertNotIn("services.import_staging_service", source)

        services_init = (
            ROOT / "fnos_media_import" / "services" / "__init__.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("__getattr__", services_init)
        self.assertNotIn("_EXPORTS", services_init)

    def test_rclone_and_organizer_completion_fallbacks_are_deleted(self) -> None:
        services = ROOT / "fnos_media_import" / "services"
        for name in (
            "organizer_dispatch_service.py",
            "rclone_category_finalizer.py",
            "rclone_ready_items_completion_service.py",
            "rclone_run_import_finalizer.py",
            "rclone_scheduler.py",
        ):
            with self.subTest(name=name):
                self.assertFalse((services / name).exists())

        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "fnos_media_import").rglob("*.py")
        )
        for retired in (
            "OrganizerDispatchService",
            "set_run_ready_handler",
            "finalize_category_imports",
            "recover_waiting_organizer_dispatches",
            "recover_unstarted_staging_jobs",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, production)

        script = (ROOT / "scripts" / "fnos_rclone_worker.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("notify_legacy_file_completion", script)
        self.assertIn(
            'if [ "$RCLONE_COPY_MODE" != "true" ]; then',
            script,
        )
        self.assertIn("拒绝全局目录扫描", script)
        for retired_worker_function in (
            "process_job() {",
            "download_file() {",
            "upload_file() {",
            "cmcc_upload_file() {",
            "resolve_root_resource_dir() {",
            "build_naming_plan() {",
        ):
            with self.subTest(retired_worker_function=retired_worker_function):
                self.assertNotIn(retired_worker_function, script)
        self.assertNotIn("RCLONE_STAGING_ENABLED", script)
        self.assertEqual(1, script.count('main "$@"'))
        self.assertLess(script.index("trap 'request_stop'"), script.index('main "$@"'))

    def test_execution_path_mapping_uses_only_frozen_plan_paths(self) -> None:
        plan = {
            "paths": {
                "destination_staging": {
                    "namespace_id": "openlist:main:staging",
                    "path": "/staging/movie/job-42",
                },
                "organizer": {
                    "namespace_id": "openlist:main:library",
                    "path": "/library/movie/job-42",
                },
            }
        }

        self.assertEqual(
            map_execution_path_to_organizer(
                "/staging/movie/job-42/Feature/Movie.mkv",
                plan,
            ),
            "/library/movie/job-42/Feature/Movie.mkv",
        )
        self.assertEqual(
            map_execution_path_to_organizer("/staging/movie/job-42", plan),
            "/library/movie/job-42",
        )

    def test_execution_path_mapping_rejects_outside_or_unsafe_paths(self) -> None:
        plan = {
            "paths": {
                "destination_staging": {"path": "/staging/movie/job-42"},
                "organizer": {"path": "/library/movie/job-42"},
            }
        }

        self.assertEqual(
            map_execution_path_to_organizer("/staging/movie/job-420/file.mkv", plan),
            "",
        )
        self.assertEqual(
            map_execution_path_to_organizer(
                "/staging/movie/job-42/../outside.mkv",
                plan,
            ),
            "",
        )

    def test_unregistered_pre_v1_app_handlers_are_deleted(self) -> None:
        source = (ROOT / "fnos_media_import" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("兼容 re-export", source)
        for handler in (
            "def api_search(",
            "def api_detect(",
            "def api_import(",
            "def api_jobs(",
            "def api_quark_check(",
            "def api_quark_file_list(",
            "def api_cloud139_check(",
            "def api_cloud139_file_list(",
        ):
            with self.subTest(handler=handler):
                self.assertNotIn(handler, source)
        for retired_reexport in (
            "_absolute_media_dirs",
            "_common_top_directory",
            "_btbtla_inspection_summary",
            "_extract_url_candidate",
        ):
            with self.subTest(retired_reexport=retired_reexport):
                self.assertNotIn(retired_reexport, source)

    def test_business_services_receive_narrow_repository_slices(self) -> None:
        expected_types = {
            "organizer/service.py": "OrganizerRepositories",
            "services/rclone_service.py": "RcloneRepositories",
            "services/job_service.py": "JobRepositories",
            "services/update_service.py": "UpdateRepositories",
            "notifications/scheduler.py": "NotificationRepositories",
            "services/notification_settings_service.py": "NotificationRepositories",
        }
        package = ROOT / "fnos_media_import"
        for relative, repository_type in expected_types.items():
            source = (package / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertIn(repository_type, source)
                self.assertNotIn("from ..database import Database", source)
                self.assertNotIn(": RepositorySet", source)

        app_source = (package / "app.py").read_text(encoding="utf-8")
        for factory in (
            "OrganizerRepositories.from_set(db)",
            "RcloneRepositories.from_set(db)",
            "JobRepositories.from_set(db)",
            "UpdateRepositories.from_set(db)",
            "NotificationRepositories.from_set(db)",
        ):
            with self.subTest(factory=factory):
                self.assertIn(factory, app_source)

        runtime_source = (package / "runtime_builder.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "OrganizerService(\n            self._organizer_repositories,",
            runtime_source,
        )
        self.assertNotIn(
            "OrganizerService(\n            self._database,",
            runtime_source,
        )


if __name__ == "__main__":
    unittest.main()
