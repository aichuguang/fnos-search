from __future__ import annotations

from dataclasses import dataclass
import os
import threading
from typing import Any, Callable

from .importers.cloud139 import Cloud139Importer
from .importers.quark import ActiveQuarkImporter, QuarkImporter
from .importers.sixpan import SixPanOfflineImporter
from .importers.tianyi import TianyiImporter
from .media.fnos import FnosMediaRefresher
from .organizer.service import OrganizerService
from .providers.btbtla import BtbtlaClient
from .providers.pansou import PanSouClient
from .search import BtbtlaProvider, PanSouProvider, SearchAggregator, SearchProviderConfig
from .services.import_service import ImportService
from .services.search_service import SearchService


@dataclass(frozen=True)
class RuntimeBuild:
    pansou: PanSouClient
    btbtla: BtbtlaClient
    quark_importer: QuarkImporter
    cloud139_importer: Cloud139Importer
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


def organizer_defer_media_refresh(
    config: Any,
    openlist_config_provider: Callable[[], dict[str, Any]] | None = None,
) -> bool:
    organizer = config.raw.get("organizer", {}) if isinstance(config.raw.get("organizer"), dict) else {}
    # V1 planning validates the concrete OpenList mount/connection before a
    # transfer can start.  Runtime refresh deferral therefore follows the
    # Organizer feature flag, not a removed process-wide OpenList URL.  The
    # optional provider is retained only for API compatibility.
    _ = openlist_config_provider
    return bool(
        organizer.get("enabled", False)
        and organizer.get("staging_enabled", True)
    )


def rclone_runtime_config(
    config: Any,
    openlist_config_provider: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runtime = dict(config.raw.get("rclone", {}) or {})
    for legacy_key in ("execution_mode", "container_name", "exec_user"):
        runtime.pop(legacy_key, None)
    runtime["config_path"] = str(
        os.getenv("RCLONE_CONFIG_PATH") or runtime.get("config_path") or "/config/rclone/rclone.conf"
    ).strip()
    runtime["cache_dir"] = str(
        os.getenv("RCLONE_CACHE_DIR") or runtime.get("cache_dir") or "/cache"
    ).strip()
    defer_refresh = organizer_defer_media_refresh(config, openlist_config_provider)
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
    if staging_enabled:
        # V1 任务级搬运只能由固化 execution_plan 触发。即使旧数据库还保留
        # 了分类级定时分钟，也应在运行时关闭，避免周期性空跑和告警。
        runtime["auto_interval_minutes"] = 0
    runtime["staging_dir_name"] = str(organizer.get("staging_dir_name") or "_入库暂存").strip()
    return runtime


class RuntimeBuilder:
    def __init__(
        self,
        database: Any,
        token_update_callback: Callable[[dict[str, Any]], bool],
        owner_id: str = "",
        quark_resolver: Callable[[], Any] | None = None,
        openlist_config_provider: Callable[[], dict[str, Any]] | None = None,
        organizer_openlist_client_resolver: Callable[[dict[str, Any], dict[str, Any]], Any]
        | None = None,
    ) -> None:
        self._database = database
        self._token_update_callback = token_update_callback
        self._owner_id = str(owner_id or "").strip()
        self._quark_resolver = quark_resolver if callable(quark_resolver) else None
        self._openlist_config_provider = (
            openlist_config_provider if callable(openlist_config_provider) else None
        )
        self._organizer_openlist_client_resolver = (
            organizer_openlist_client_resolver
            if callable(organizer_openlist_client_resolver)
            else None
        )

    def set_quark_resolver(self, resolver: Callable[[], Any] | None) -> None:
        """Bind the account-aware Quark resolver for every future runtime build.

        ``RuntimeBuilder`` is also used by the hot-reload path.  A resolver set
        only on the first ``ActiveQuarkImporter`` instance would be lost when a
        new runtime is built, causing the wrapper to silently fall back to the
        legacy global ``quark`` configuration.  Keeping the callback on the
        builder makes initial and replacement builds use the same account
        selection logic.
        """

        self._quark_resolver = resolver if callable(resolver) else None

    def set_openlist_config_provider(
        self, provider: Callable[[], dict[str, Any]] | None
    ) -> None:
        self._openlist_config_provider = provider if callable(provider) else None

    def set_organizer_openlist_client_resolver(
        self,
        resolver: Callable[[dict[str, Any], dict[str, Any]], Any] | None,
    ) -> None:
        """Bind the task-scoped frozen OpenList resolver for future builds."""

        self._organizer_openlist_client_resolver = resolver if callable(resolver) else None

    def _openlist_config(self) -> dict[str, Any]:
        if not callable(self._openlist_config_provider):
            return {}
        try:
            value = self._openlist_config_provider()
        except Exception:  # noqa: BLE001
            return {}
        return dict(value or {}) if isinstance(value, dict) else {}

    @staticmethod
    def _runtime_config(config: Any, openlist: dict[str, Any]) -> Any:
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
        supplied = openlist if isinstance(openlist, dict) else {}
        raw["openlist"] = {
            key: value
            for source in (configured, supplied)
            for key, value in source.items()
            if key in allowed_openlist_keys
        }
        config_type = type(config)
        try:
            return config_type(raw=raw, base_dir=getattr(config, "base_dir", None))
        except TypeError:
            return config_type(raw)

    def _build_quark_importer(self, config: Any) -> ActiveQuarkImporter:
        # Quark credentials are resolved exclusively from the active Provider
        # account.  The fallback is intentionally empty and can never use a
        # removed global auto-save URL/token section.
        importer = ActiveQuarkImporter(QuarkImporter({}))
        if self._quark_resolver is not None:
            importer.set_resolver(self._quark_resolver)
        return importer

    def build(self, config: Any, *, recover_background: bool = True) -> RuntimeBuild:
        runtime_config = self._runtime_config(config, self._openlist_config())
        # Search source enablement and destination routing are V1 database
        # concerns.  The search adapters keep a route argument for their
        # low-level API shape, but must never receive the removed global
        # ``config.routes`` section.
        pansou = PanSouClient(runtime_config.raw["pansou"], {})
        btbtla = BtbtlaClient(runtime_config.raw.get("btbtla", {}), {})
        quark = self._build_quark_importer(runtime_config)
        # 139 credentials/target paths are resolved from Provider account and
        # storage-target bindings.  Keep an empty shell for legacy service
        # interfaces until their callers are fully migrated; it is always
        # reported as unconfigured rather than reading global fields.
        cloud139 = Cloud139Importer({}, cmcc_config={})
        generic = {
            # Cloud189 is the historical source name for Tianyi.  Do not
            # construct a generic Webhook from global endpoint/token values;
            # the active Tianyi Provider account is supplied by V1 planning.
            "cloud189": TianyiImporter({}),
            # ``sixpan`` contains only non-secret polling/parse defaults in
            # V1.  Client identity and tokens are resolved from the active
            # Provider account by the V1 execution adapter.
            "sixpan": SixPanOfflineImporter(
                runtime_config.raw.get("sixpan", {}),
                token_update_callback=self._token_update_callback,
            ),
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
        importer = ImportService(
            self._database,
            runtime_config,
            quark,
            fnos,
            cloud139_importer=cloud139,
            generic_importers=generic,
        )
        organizer = OrganizerService(
            self._database,
            runtime_config.raw,
            runtime_config.categories,
            fnos,
            recover_on_startup=recover_background,
            owner_id=self._owner_id,
        )
        if self._organizer_openlist_client_resolver is not None:
            organizer.set_openlist_client_resolver(
                self._organizer_openlist_client_resolver
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
