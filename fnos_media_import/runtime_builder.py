from __future__ import annotations

from dataclasses import dataclass
import os
import threading
from typing import Any, Callable

from .config import AppConfig
from .providers.active import ActiveProviderImporter, ActiveProviderUnavailable
from .classifiers.link_classifier import detect_link
from .media.fnos import FnosMediaRefresher
from .organizer.service import OrganizerService
from .planning.destination_resolver import DestinationResolver
from .planning.plan_validator import PlanValidationDeps, validate_plan
from .planning.planner import ImportPlanner
from .planning.source_resolver import SourceResolver
from .providers.btbtla import BtbtlaClient
from .providers.pansou import PanSouClient
from .repositories.repository_set import OrganizerRepositories
from .search import BtbtlaProvider, PanSouProvider, SearchAggregator, SearchProviderConfig
from .services.import_service import ImportService
from .services.search_service import SearchService


@dataclass(frozen=True)
class RuntimeBuild:
    pansou: PanSouClient
    btbtla: BtbtlaClient
    quark_importer: Any
    cloud139_importer: Any
    generic_importers: dict[str, Any]
    fnos: FnosMediaRefresher
    search_service: SearchService
    import_service: ImportService
    organizer_service: OrganizerService

    def close(self) -> list[str]:
        """Best-effort retirement of clients owned by this build."""
        errors: list[str] = []
        seen: set[int] = set()
        for value in (
            self.organizer_service,
            self.import_service,
            self.search_service,
            self.fnos,
            *self.generic_importers.values(),
            self.cloud139_importer,
            self.quark_importer,
            self.btbtla,
            self.pansou,
        ):
            if value is None or id(value) in seen:
                continue
            seen.add(id(value))
            closer = getattr(value, "close", None) or getattr(value, "shutdown", None)
            if not callable(closer):
                continue
            try:
                closer()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(value).__name__}: {exc}")
        return errors


def organizer_defer_media_refresh(config: Any) -> bool:
    organizer = config.raw.get("organizer", {}) if isinstance(config.raw.get("organizer"), dict) else {}
    # V1 planning validates the concrete OpenList mount/connection before a
    # transfer can start.  Runtime refresh deferral therefore follows the
    # Organizer feature flag, not a removed process-wide OpenList URL.  The
    return bool(
        organizer.get("enabled", False)
        and organizer.get("staging_enabled", True)
    )


def rclone_runtime_config(config: Any) -> dict[str, Any]:
    runtime = dict(config.raw.get("rclone", {}) or {})
    runtime["config_path"] = str(
        os.getenv("RCLONE_CONFIG_PATH") or runtime.get("config_path") or "/config/rclone/rclone.conf"
    ).strip()
    runtime["cache_dir"] = str(
        os.getenv("RCLONE_CACHE_DIR") or runtime.get("cache_dir") or "/cache"
    ).strip()
    defer_refresh = organizer_defer_media_refresh(config)
    runtime["defer_media_refresh_to_organizer"] = defer_refresh
    if defer_refresh:
        # 新流程由 OpenList 生成并同步 STRM，飞牛监听本地目录变化即可。
        # 即使旧配置曾开启 worker 刷新，也不能在 Organizer 整理前抢先扫描。
        runtime["refresh_in_worker"] = "false"
    organizer = config.raw.get("organizer", {}) if isinstance(config.raw.get("organizer"), dict) else {}
    staging_enabled = bool(
        defer_refresh
        and organizer.get("staging_enabled", True)
    )
    runtime["staging_enabled"] = staging_enabled
    runtime["staging_dir_name"] = str(organizer.get("staging_dir_name") or "_入库暂存").strip()
    return runtime


class RuntimeBuilder:
    def __init__(
        self,
        database: Any,
        *,
        organizer_repositories: OrganizerRepositories,
        owner_id: str = "",
        organizer_openlist_client_resolver: Callable[[dict[str, Any], dict[str, Any]], Any]
        | None = None,
        provider_importer_resolver: Callable[[str], Any] | None = None,
        accounts_service: Any = None,
        target_service: Any = None,
        mount_service: Any = None,
        rules_service: Any = None,
        provider_registry: Any = None,
        source_record_service: Any = None,
        execution_repository: Any = None,
        enqueue_transfer: Callable[[int], Any] | None = None,
        enqueue_transfer_with_connection: Callable[[Any, int], Any] | None = None,
        config_revision_provider: Callable[[], int] | None = None,
    ) -> None:
        self._database = database
        self._organizer_repositories = organizer_repositories
        self._owner_id = str(owner_id or "").strip()
        self._organizer_openlist_client_resolver = (
            organizer_openlist_client_resolver
            if callable(organizer_openlist_client_resolver)
            else None
        )
        self._provider_importer_resolver = (
            provider_importer_resolver
            if callable(provider_importer_resolver)
            else None
        )
        self._accounts_service = accounts_service
        self._target_service = target_service
        self._mount_service = mount_service
        self._rules_service = rules_service
        self._provider_registry = provider_registry
        self._source_record_service = source_record_service
        self._execution_repository = execution_repository
        self._enqueue_transfer = enqueue_transfer
        self._enqueue_transfer_with_connection = enqueue_transfer_with_connection
        self._config_revision_provider = config_revision_provider

    @staticmethod
    def _runtime_config(config: Any) -> Any:
        """Return a config view containing only non-identity OpenList tuning."""

        raw = dict(getattr(config, "raw", {}) or {})
        allowed_openlist_keys = {
            "timeout",
            "batch_timeout",
            "verify_tls",
            "list_refresh_default",
            "use_env_proxy",
            "request_retries",
            "retry_backoff_seconds",
        }
        configured = raw.get("openlist") if isinstance(raw.get("openlist"), dict) else {}
        raw["openlist"] = {
            key: value
            for key, value in configured.items()
            if key in allowed_openlist_keys
        }
        return AppConfig(raw=raw, base_dir=config.base_dir)

    def _active_importer(
        self, provider_kind: str, display_name: str
    ) -> ActiveProviderImporter:
        def resolve() -> Any:
            if self._provider_importer_resolver is not None:
                return self._provider_importer_resolver(provider_kind)
            raise ActiveProviderUnavailable(
                f"当前{display_name}账号未配置或未激活"
            )

        return ActiveProviderImporter(display_name, resolve)

    def _build_quark_importer(self) -> ActiveProviderImporter:
        return self._active_importer("quark", "夸克")

    def _require_v1_dependencies(self) -> None:
        missing = [
            name
            for name, value in (
                ("accounts_service", self._accounts_service),
                ("target_service", self._target_service),
                ("mount_service", self._mount_service),
                ("rules_service", self._rules_service),
                ("provider_registry", self._provider_registry),
                ("provider_importer_resolver", self._provider_importer_resolver),
                (
                    "organizer_openlist_client_resolver",
                    self._organizer_openlist_client_resolver,
                ),
                ("source_record_service", self._source_record_service),
                ("execution_repository", self._execution_repository),
                ("enqueue_transfer", self._enqueue_transfer),
                (
                    "enqueue_transfer_with_connection",
                    self._enqueue_transfer_with_connection,
                ),
            )
            if value is None
        ]
        if missing:
            raise RuntimeError(
                "RuntimeBuilder V1 dependencies missing: " + ", ".join(missing)
            )

    def build(self, config: Any, *, recover_background: bool = True) -> RuntimeBuild:
        self._require_v1_dependencies()
        runtime_config = self._runtime_config(config)
        # Search source enablement and destination routing are V1 database
        # concerns.  The search adapters keep a route argument for their
        # low-level API shape, but must never receive the removed global
        # ``config.routes`` section.
        pansou = PanSouClient(runtime_config.raw["pansou"], {})
        btbtla = BtbtlaClient(runtime_config.raw.get("btbtla", {}), {})
        quark = self._build_quark_importer()
        cloud139 = self._active_importer("mobile", "移动云")
        generic = {
            "cloud189": self._active_importer("tianyi", "天翼云"),
            "sixpan": self._active_importer("sixpan", "六盘"),
        }
        fnos = FnosMediaRefresher(runtime_config.raw["fnos"])
        search_config = runtime_config.raw.get("search", {})
        providers = search_config.get("providers", {}) if isinstance(search_config.get("providers"), dict) else {}
        pansou_config = providers.get("pansou", {}) if isinstance(providers.get("pansou"), dict) else {}
        btbtla_config = providers.get("btbtla", {}) if isinstance(providers.get("btbtla"), dict) else {}
        aggregator = SearchAggregator(
            [
                PanSouProvider(pansou, SearchProviderConfig("pansou", "PanSou", _as_bool(pansou_config.get("enabled"), True), _as_int(pansou_config.get("priority"), 10))),
                BtbtlaProvider(btbtla, SearchProviderConfig("btbtla", "BTBTLA 磁链搜索", _as_bool(btbtla_config.get("enabled"), True), _as_int(btbtla_config.get("priority"), 30))),
            ],
            aliases=search_config.get("aliases", {}),
        )
        search = SearchService(self._database, aggregator)
        source_resolver = SourceResolver(
            detect_link=lambda url, password="", **_kwargs: detect_link(
                url, {}, password=password
            ),
            config=runtime_config.raw,
            source_catalog_provider=self._provider_registry.search_sources,
        )
        destination_resolver = DestinationResolver(
            self._rules_service,
            self._target_service,
            self._accounts_service,
        )
        planner = ImportPlanner(
            accounts_service=self._accounts_service,
            target_service=self._target_service,
            rules_service=self._rules_service,
            mount_service=self._mount_service,
            registry=self._provider_registry,
            config=runtime_config.raw,
            destination_resolver=destination_resolver,
        )
        plan_deps = PlanValidationDeps(
            accounts_service=self._accounts_service,
            target_service=self._target_service,
            mount_service=self._mount_service,
            registry=self._provider_registry,
            source_record_service=self._source_record_service,
            assignment_service=self._rules_service,
            config=runtime_config.raw,
        )

        importer = ImportService(
            database=self._database,
            config=runtime_config,
            fnos=fnos,
            source_resolver=source_resolver,
            destination_resolver=destination_resolver,
            plan_import=planner.plan_import,
            validate_plan=validate_plan,
            plan_validation_deps=plan_deps,
            execution_repository=self._execution_repository,
            enqueue_transfer=self._enqueue_transfer,
            enqueue_transfer_with_connection=self._enqueue_transfer_with_connection,
            source_record_service=self._source_record_service,
            config_revision_provider=self._config_revision_provider,
            executor_id=self._owner_id,
        )
        organizer = OrganizerService(
            self._organizer_repositories,
            runtime_config.raw,
            runtime_config.categories,
            fnos,
            recover_on_startup=recover_background,
            owner_id=self._owner_id,
            openlist_client_resolver=self._organizer_openlist_client_resolver,
            execution_repository=self._execution_repository,
        )
        if not recover_background:
            # Web 进程仍可持久化 Organizer task，但不能在本进程启动
            # Timer 扫描；worker/all 进程会通过启动恢复或热重载激活它。
            organizer.suspend_background()
        return RuntimeBuild(pansou, btbtla, quark, cloud139, generic, fnos, search, importer, organizer)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


class RuntimeRetirementQueue:
    """Closes replaced runtime builds after a grace period for in-flight requests."""

    def __init__(self, grace_seconds: float = 300.0) -> None:
        self._grace_seconds = max(0.0, float(grace_seconds))
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[RuntimeBuild, threading.Timer]] = {}

    def retire(self, build: RuntimeBuild) -> None:
        timer = threading.Timer(self._grace_seconds, self._close, args=(build,))
        timer.daemon = True
        with self._lock:
            self._pending[id(build)] = (build, timer)
        timer.start()

    def close_all(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for build, timer in pending:
            timer.cancel()
            build.close()

    def _close(self, build: RuntimeBuild) -> None:
        with self._lock:
            item = self._pending.pop(id(build), None)
        if item is not None:
            build.close()


class RuntimeInstaller:
    """Install lifecycle references owned outside an immutable RuntimeBuild.

    It may update the current Organizer reference and shared locator runtime,
    but never reaches into ImportService or mutates nested planning fields.
    """

    def __init__(self, *, locator_resolver: Any, transfer_dispatch: Any) -> None:
        self.locator_resolver = locator_resolver
        self.transfer_dispatch = transfer_dispatch

    def install(self, build: RuntimeBuild, config: Any) -> None:
        locator_config = rclone_runtime_config(config)
        locator_config["relay_root"] = os.getenv("FNOS_RELAY_ROOT", "/data/relay")
        self.locator_resolver.update_config(locator_config)
        self.transfer_dispatch.set_organizer_service(build.organizer_service)
