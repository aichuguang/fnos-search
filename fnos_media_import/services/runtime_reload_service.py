from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..runtime import RuntimeSnapshot
from ..runtime_builder import rclone_runtime_config


@dataclass(frozen=True)
class RuntimeReloadResult:
    config: Any
    build: Any
    security_config: dict[str, Any]
    runtime_revision: int
    response: dict[str, Any]


def finalize_organizer_runtime_transition(
    *,
    dispatcher: Any,
    previous_build: Any,
    candidate_build: Any,
    retirement: Any,
    activate_background: bool = True,
) -> None:
    """Publish the Organizer only after the app has updated its closures."""

    dispatcher_setter = getattr(dispatcher, "set_organizer", None)
    if callable(dispatcher_setter):
        dispatcher_setter(candidate_build.organizer_service)
    old_organizer = getattr(previous_build, "organizer_service", None)
    suspend_old = getattr(old_organizer, "suspend_background", None)
    if callable(suspend_old):
        suspend_old()
    new_organizer = getattr(candidate_build, "organizer_service", None)
    activate_new = getattr(new_organizer, "activate_background_recovery", None)
    if activate_background and callable(activate_new):
        activate_new(include_scanning=False)
    retirement.retire(previous_build)


class RuntimeReloadService:
    """Builds, applies and atomically publishes a new runtime snapshot."""

    def __init__(
        self,
        *,
        load_config: Callable[[], Any],
        builder: Any,
        runtime_services: Any,
        retirement: Any,
        database: Any,
        job_service: Any,
        rclone_service: Any,
        update_service: Any,
        update_scheduler: Any,
        trending_scheduler: Any,
        config_bool: Callable[[dict[str, Any], str, bool], bool],
        config_int: Callable[[dict[str, Any], str, int], int],
        rollback_logger: Callable[[], None],
        advanced_config_key: str,
        organizer_dispatcher: Any | None = None,
    ) -> None:
        self.load_config = load_config
        self.builder = builder
        self.runtime_services = runtime_services
        self.retirement = retirement
        self.database = database
        self.job_service = job_service
        self.rclone_service = rclone_service
        self.update_service = update_service
        self.update_scheduler = update_scheduler
        self.trending_scheduler = trending_scheduler
        self.config_bool = config_bool
        self.config_int = config_int
        self.rollback_logger = rollback_logger
        self.advanced_config_key = advanced_config_key
        # Kept as an optional constructor argument for compatibility with
        # existing composition tests. Dispatcher publication belongs to the
        # app closure, after its config/service references have been updated.
        _ = organizer_dispatcher

    def reload(self, current_config: Any, current_build: Any) -> RuntimeReloadResult:
        reloaded_config = self.load_config()
        _preserve_runtime_secrets(current_config, reloaded_config)
        candidate = self.builder.build(reloaded_config, recover_background=False)
        try:
            self._apply_mutable_runtime(reloaded_config, candidate)
        except Exception:
            try:
                self._apply_mutable_runtime(current_config, current_build)
            except Exception:  # noqa: BLE001
                self.rollback_logger()
            candidate.close()
            raise

        revision = self.runtime_services.swap(
            RuntimeSnapshot(
                config=reloaded_config,
                database=self.database,
                pansou=candidate.pansou,
                btbtla=candidate.btbtla,
                quark_importer=candidate.quark_importer,
                cloud139_importer=candidate.cloud139_importer,
                generic_importers=candidate.generic_importers,
                fnos=candidate.fnos,
                search_service=candidate.search_service,
                import_service=candidate.import_service,
                organizer_service=candidate.organizer_service,
                job_service=self.job_service,
                rclone_service=self.rclone_service,
                update_service=self.update_service,
                update_scheduler=self.update_scheduler,
            )
        )
        response = {
            "success": True,
            "message": "运行配置已重载",
            "advanced_config_key": self.advanced_config_key,
            "runtime_revision": revision,
            "rclone": self.rclone_service.status(),
            "organizer": candidate.organizer_service.status(),
            "update_scheduler": _component_status(self.update_scheduler),
            "trending_discovery": _component_status(self.trending_scheduler),
            "fnos_configured": bool(candidate.fnos.describe().get("configured"))
            if hasattr(candidate.fnos, "describe")
            else False,
        }
        return RuntimeReloadResult(
            config=reloaded_config,
            build=candidate,
            security_config=reloaded_config.raw.get("security", {}),
            runtime_revision=revision,
            response=response,
        )

    def _apply_mutable_runtime(self, config: Any, build: Any) -> None:
        scheduler = config.raw.get("update_scheduler", {})
        scheduler = scheduler if isinstance(scheduler, dict) else {}
        self.update_service.set_runtime(
            config=config.raw,
            categories=config.categories,
            search_service=build.search_service,
            import_service=build.import_service,
            quark_importer=build.quark_importer,
            cloud139_importer=build.cloud139_importer,
        )
        self.update_scheduler.apply_config(
            enabled=self.config_bool(scheduler, "enabled", True),
            interval_seconds=self.config_int(scheduler, "interval_seconds", 60),
            max_subscriptions_per_tick=self.config_int(scheduler, "max_subscriptions_per_tick", 5),
            coalesce_missed_runs=self.config_bool(scheduler, "coalesce_missed_runs", True),
        )
        hot_discovery = config.raw.get("hot_discovery", {})
        hot_discovery = hot_discovery if isinstance(hot_discovery, dict) else {}
        self.trending_scheduler.apply_config(
            enabled=self.config_bool(hot_discovery, "enabled", False),
            run_at=str(hot_discovery.get("run_at") or "08:30"),
        )
        self.rclone_service.apply_runtime_config(
            rclone_runtime_config(config),
            config.raw["fnos"],
            config.categories,
            config.raw.get("cmcc_upload", {}),
            config.raw.get("cloud139", {}),
        )


def _component_status(component: Any) -> dict[str, Any]:
    status = getattr(component, "status", None)
    if not callable(status):
        return {}
    value = status()
    return dict(value) if isinstance(value, dict) else {}


def _preserve_runtime_secrets(current_config: Any, reloaded_config: Any) -> None:
    """Keep process-scoped secrets that are resolved after the base config loads."""
    current_raw = current_config.raw if isinstance(getattr(current_config, "raw", None), dict) else {}
    reloaded_raw = reloaded_config.raw if isinstance(getattr(reloaded_config, "raw", None), dict) else {}

    current_app = current_raw.get("app") if isinstance(current_raw.get("app"), dict) else {}
    secret_key = str(current_app.get("secret_key") or "").strip()
    if secret_key:
        reloaded_raw.setdefault("app", {})["secret_key"] = secret_key

    current_security = current_raw.get("security") if isinstance(current_raw.get("security"), dict) else {}
    ip_hash_salt = str(current_security.get("ip_hash_salt") or "").strip()
    if ip_hash_salt:
        reloaded_raw.setdefault("security", {})["ip_hash_salt"] = ip_hash_salt
