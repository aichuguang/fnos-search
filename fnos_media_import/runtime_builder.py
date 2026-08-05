from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from .importers.cloud139 import Cloud139Importer
from .importers.generic import GenericWebhookImporter
from .importers.quark import QuarkImporter
from .importers.sixpan import SixPanOfflineImporter
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


def organizer_defer_media_refresh(config: Any) -> bool:
    organizer = config.raw.get("organizer", {}) if isinstance(config.raw.get("organizer"), dict) else {}
    openlist = config.raw.get("openlist", {}) if isinstance(config.raw.get("openlist"), dict) else {}
    return bool(organizer.get("enabled", False) and str(openlist.get("base_url") or "").strip())


def rclone_runtime_config(config: Any) -> dict[str, Any]:
    runtime = dict(config.raw.get("rclone", {}) or {})
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
    if staging_enabled:
        # 任务级暂存只能由带固化 staging_plan 的 Job 触发。即使旧数据库
        # 还保留了分类级定时分钟，也应在运行时关闭，避免周期性空跑和告警。
        runtime["auto_interval_minutes"] = 0
    runtime["staging_dir_name"] = str(organizer.get("staging_dir_name") or "_入库暂存").strip()
    return runtime


class RuntimeBuilder:
    def __init__(
        self,
        database: Any,
        token_update_callback: Callable[[dict[str, Any]], bool],
        owner_id: str = "",
    ) -> None:
        self._database = database
        self._token_update_callback = token_update_callback
        self._owner_id = str(owner_id or "").strip()

    def build(self, config: Any, *, recover_background: bool = True) -> RuntimeBuild:
        pansou = PanSouClient(config.raw["pansou"], config.raw.get("routes", {}))
        btbtla = BtbtlaClient(config.raw.get("btbtla", {}), config.raw.get("routes", {}))
        quark = QuarkImporter(config.raw["quark"])
        cloud139_config = dict(config.raw.get("cloud139", {}) or {})
        if organizer_defer_media_refresh(config):
            cloud139_config["refresh_after_submit"] = False
        cloud139 = Cloud139Importer(cloud139_config, cmcc_config=config.raw.get("cmcc_upload", {}))
        generic = {
            "cloud189": GenericWebhookImporter("天翼云", config.raw.get("cloud189", {})),
            "sixpan": SixPanOfflineImporter(config.raw.get("sixpan", {}), token_update_callback=self._token_update_callback),
        }
        fnos = FnosMediaRefresher(config.raw["fnos"])
        search_config = config.raw.get("search", {})
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
        importer = ImportService(self._database, config, quark, fnos, cloud139_importer=cloud139, generic_importers=generic)
        organizer = OrganizerService(
            self._database,
            config.raw,
            config.categories,
            fnos,
            recover_on_startup=recover_background,
            owner_id=self._owner_id,
        )
        if not recover_background:
            # Web-only 进程仍可持久化 Organizer task，但不能在本进程启动
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
