from __future__ import annotations

import re
from typing import Any

from ..constants import ROUTE_CLOUD139_DIRECT, ROUTE_QUARK_TO_MOBILE, ROUTE_SIXPAN_OFFLINE
from ..storage_paths import cmcc_upload_root, openlist_root_for_upload, rclone_upload_root, upload_backend


DEFAULT_STAGING_DIR_NAME = "_入库暂存"
STAGING_PLAN_VERSION = 2


def normalize_staging_segment(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if not text or text in {".", ".."} or "/" in text:
        return DEFAULT_STAGING_DIR_NAME
    text = re.sub(r'[\x00-\x1f<>:"|?*]+', "", text).strip(" .")
    return text or DEFAULT_STAGING_DIR_NAME


def staging_category_root(final_category_root: Any, *, category_label: Any = "", staging_dir_name: Any = "") -> str:
    """Create a staging category next to the configured final category.

    Examples:
    - ``/移动云/电视剧`` -> ``/移动云/_入库暂存/电视剧``
    - ``移动云盘A/电视剧`` -> ``移动云盘A/_入库暂存/电视剧``
    - ``/电视剧`` -> ``/_入库暂存/电视剧``
    """

    raw = str(final_category_root or "").strip().replace("\\", "/")
    leading_slash = raw.startswith("/")
    parts = [part for part in raw.strip("/").split("/") if part]
    label = str(category_label or "").strip().replace("\\", "/").strip("/")
    leaf = parts[-1] if parts else label
    if not leaf:
        return ""
    parent = parts[:-1]
    staging_segment = normalize_staging_segment(staging_dir_name)
    if staging_segment.casefold() == leaf.casefold():
        staging_segment = DEFAULT_STAGING_DIR_NAME
    if staging_segment.casefold() == leaf.casefold():
        staging_segment = "_任务暂存"
    result = "/".join([*parent, staging_segment, leaf])
    return f"/{result}" if leading_slash else result


def staging_job_root(category_root: Any, job_id: int) -> str:
    root = str(category_root or "").strip().replace("\\", "/").rstrip("/")
    if not root or int(job_id or 0) <= 0:
        return ""
    return f"{root}/job-{int(job_id)}"


def staging_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    raw_data = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
    plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
    return plan if plan.get("enabled") else {}


def validated_staging_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the immutable staging contract before any provider retry."""

    plan = staging_plan_from_job(job)
    if not plan:
        return {}
    missing: list[str] = []
    try:
        version = int(plan.get("version") or 0)
        job_id = int((job or {}).get("id") or 0)
        plan_job_id = int(plan.get("job_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化暂存计划的版本或任务 ID 无效") from exc
    if version != STAGING_PLAN_VERSION:
        missing.append(f"version={STAGING_PLAN_VERSION}")
    if job_id <= 0 or plan_job_id != job_id:
        missing.append("job_id")

    route = str(plan.get("route") or "").strip().lower()
    job_route = str((job or {}).get("target_route") or "").strip().lower()
    if route not in {ROUTE_QUARK_TO_MOBILE, ROUTE_CLOUD139_DIRECT, ROUTE_SIXPAN_OFFLINE}:
        missing.append("route")
    elif job_route and route != job_route:
        missing.append("route_match")
    category = str(plan.get("category") or "").strip().lower()
    job_category = str((job or {}).get("category") or "").strip().lower()
    if not category or (job_category and category != job_category):
        missing.append("category")

    job_dir_name = str(plan.get("job_dir_name") or "").strip()
    expected_job_dir = f"job-{job_id}" if job_id > 0 else ""
    if job_dir_name != expected_job_dir:
        missing.append("job_dir_name")

    required_paths = (
        "provider_target_path",
        "storage_final_category_root",
        "storage_staging_category_root",
        "storage_job_root",
        "openlist_final_category_root",
        "openlist_staging_category_root",
        "openlist_job_root",
    )
    normalized_paths = {key: _normalize_path(plan.get(key)) for key in required_paths}
    for key, value in normalized_paths.items():
        if not value:
            missing.append(key)
        elif _has_unsafe_path_segments(value):
            missing.append(f"{key}_path_segments")
    if "openlist_refresh_prefix" not in plan:
        missing.append("openlist_refresh_prefix")
    elif _has_unsafe_path_segments(plan.get("openlist_refresh_prefix")):
        missing.append("openlist_refresh_prefix_path_segments")

    expected_storage_job_root = _normalize_path(
        staging_job_root(normalized_paths.get("storage_staging_category_root"), job_id)
    )
    if normalized_paths.get("storage_job_root") != expected_storage_job_root:
        missing.append("storage_job_root_job")
    expected_openlist_job_root = _normalize_path(
        staging_job_root(normalized_paths.get("openlist_staging_category_root"), job_id)
    )
    if normalized_paths.get("openlist_job_root") != expected_openlist_job_root:
        missing.append("openlist_job_root_job")

    backend = str(plan.get("storage_backend") or "").strip().lower()
    if route == ROUTE_QUARK_TO_MOBILE:
        if backend not in {"cmcc_api", "webdav"}:
            missing.append("storage_backend")
        quark_source_category_root = _normalize_path(plan.get("quark_source_category_root"))
        if not quark_source_category_root:
            missing.append("quark_source_category_root")
        elif _has_unsafe_path_segments(quark_source_category_root):
            missing.append("quark_source_category_root_path_segments")
        expected_quark_job_root = _normalize_path(staging_job_root(quark_source_category_root, job_id))
        quark_job_root = _normalize_path(plan.get("quark_job_root"))
        if _has_unsafe_path_segments(quark_job_root):
            missing.append("quark_job_root_path_segments")
        if quark_job_root != expected_quark_job_root:
            missing.append("quark_job_root_job")
        if normalized_paths.get("provider_target_path") != quark_job_root:
            missing.append("quark_job_root")
    elif route == ROUTE_CLOUD139_DIRECT:
        if backend != "cmcc_api":
            missing.append("storage_backend")
        if normalized_paths.get("provider_target_path") != normalized_paths.get("storage_job_root"):
            missing.append("provider_target_path")
    elif route == ROUTE_SIXPAN_OFFLINE:
        if backend != "sixpan_offline":
            missing.append("storage_backend")
        if normalized_paths.get("provider_target_path") != normalized_paths.get("storage_job_root"):
            missing.append("provider_target_path")
    if missing:
        raise ValueError(f"持久化暂存计划缺少或包含无效字段：{', '.join(dict.fromkeys(missing))}")
    return plan


def rclone_staging_run_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Return the immutable rclone route saved with a newly created job.

    Historical jobs without an enabled staging plan deliberately return an
    empty mapping. An enabled but incomplete plan is rejected instead of
    silently falling back to mutable runtime configuration.
    """

    plan = validated_staging_plan_from_job(job)
    if not plan:
        return {}
    try:
        job_id = int((job or {}).get("id") or 0)
        plan_job_id = int(plan.get("job_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化暂存计划的任务 ID 无效") from exc
    if job_id <= 0 or plan_job_id != job_id:
        raise ValueError("持久化暂存计划与当前任务 ID 不一致")

    category = str(plan.get("category") or "").strip().lower()
    job_dir_name = str(plan.get("job_dir_name") or "").strip()
    source_category_root = str(plan.get("quark_source_category_root") or "").strip().replace("\\", "/")
    storage_staging_category_root = str(plan.get("storage_staging_category_root") or "").strip().replace("\\", "/")
    storage_backend = str(plan.get("storage_backend") or "").strip().lower()
    missing: list[str] = []
    if not category:
        missing.append("category")
    if job_dir_name != f"job-{job_id}":
        missing.append("job_dir_name")
    if not source_category_root:
        missing.append("quark_source_category_root")
    if not storage_staging_category_root:
        missing.append("storage_staging_category_root")
    if storage_backend not in {"cmcc_api", "webdav"}:
        missing.append("storage_backend")
    if missing:
        raise ValueError(f"持久化暂存计划缺少或包含无效字段：{', '.join(missing)}")
    return {
        "job_id": job_id,
        "category": category,
        "category_label": str(plan.get("category_label") or "").strip(),
        "job_dir_name": job_dir_name,
        "source_category_root": source_category_root,
        "storage_staging_category_root": storage_staging_category_root,
        "storage_backend": storage_backend,
    }


def map_staging_path_to_openlist(path: Any, plan: dict[str, Any] | None) -> str:
    plan = plan if isinstance(plan, dict) else {}
    value = _normalize_path(path)
    source_root = _normalize_path(plan.get("storage_staging_category_root"))
    visible_root = _normalize_path(plan.get("openlist_staging_category_root"))
    if (
        not value
        or not source_root
        or not visible_root
        or _has_unsafe_path_segments(value)
        or _has_unsafe_path_segments(source_root)
        or _has_unsafe_path_segments(visible_root)
    ):
        return ""
    value_folded = value.casefold()
    source_folded = source_root.casefold()
    if value_folded == source_folded:
        return f"/{visible_root}"
    if value_folded.startswith(f"{source_folded}/"):
        suffix = value[len(source_root) + 1 :]
        return f"/{visible_root}/{suffix}"
    return ""


class ImportStagingService:
    """Builds immutable, per-job staging routes for newly created imports."""

    def __init__(self, config: Any) -> None:
        raw = getattr(config, "raw", {}) if config is not None else {}
        self.raw = raw if isinstance(raw, dict) else {}
        organizer = self.raw.get("organizer") if isinstance(self.raw.get("organizer"), dict) else {}
        self.organizer_config = organizer
        self.openlist_config = self.raw.get("openlist") if isinstance(self.raw.get("openlist"), dict) else {}
        self.cloud139_config = self.raw.get("cloud139") if isinstance(self.raw.get("cloud139"), dict) else {}
        self.cmcc_upload_config = self.raw.get("cmcc_upload") if isinstance(self.raw.get("cmcc_upload"), dict) else {}
        self.rclone_config = self.raw.get("rclone") if isinstance(self.raw.get("rclone"), dict) else {}
        self.sixpan_config = self.raw.get("sixpan") if isinstance(self.raw.get("sixpan"), dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(self.organizer_config.get("enabled", False) and self.organizer_config.get("staging_enabled", True))

    @property
    def openlist_configured(self) -> bool:
        return bool(str(self.openlist_config.get("base_url") or "").strip())

    @property
    def directory_name(self) -> str:
        return normalize_staging_segment(self.organizer_config.get("staging_dir_name"))

    def build(
        self,
        *,
        job_id: int,
        route: str,
        category_key: str,
        category: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            return {}
        normalized_route = str(route or "").strip().lower()
        if normalized_route not in {ROUTE_QUARK_TO_MOBILE, ROUTE_CLOUD139_DIRECT, ROUTE_SIXPAN_OFFLINE}:
            return {}
        if not self.openlist_configured:
            raise ValueError("OpenList 未配置，不能启用任务级暂存整理")
        if int(job_id or 0) <= 0:
            raise ValueError("暂存路径计划缺少有效任务 ID")

        label = str(category.get("label") or category_key or "媒体").strip()
        final_openlist_root = self._final_openlist_root(normalized_route, category, label)
        final_storage_root = self._final_storage_root(normalized_route, category, label)
        if not final_openlist_root:
            raise ValueError(f"分类 {label} 缺少 OpenList 最终目录，不能安全启用任务暂存")
        if not final_storage_root:
            raise ValueError(f"分类 {label} 缺少网盘最终目录，不能安全启用任务暂存")
        self._reject_unsafe_paths(
            storage_final_category_root=final_storage_root,
            openlist_final_category_root=final_openlist_root,
        )

        storage_staging_root = staging_category_root(
            final_storage_root,
            category_label=label,
            staging_dir_name=self.directory_name,
        )
        openlist_staging_root = staging_category_root(
            final_openlist_root,
            category_label=label,
            staging_dir_name=self.directory_name,
        )
        storage_job_path = staging_job_root(storage_staging_root, job_id)
        openlist_job_path = staging_job_root(openlist_staging_root, job_id)
        strm_refresh_prefix = self._strm_refresh_prefix(str(category_key or "").strip().lower())

        quark_source_root = str(category.get("quark_save_path") or "").strip().replace("\\", "/")
        quark_job_path = staging_job_root(quark_source_root, job_id) if quark_source_root else ""
        provider_target = quark_job_path if normalized_route == ROUTE_QUARK_TO_MOBILE else storage_job_path
        if not provider_target:
            raise ValueError(f"分类 {label} 无法生成任务级暂存目录")
        self._reject_unsafe_paths(
            provider_target_path=provider_target,
            quark_source_category_root=quark_source_root,
            quark_job_root=quark_job_path,
            storage_staging_category_root=storage_staging_root,
            storage_job_root=storage_job_path,
            openlist_staging_category_root=openlist_staging_root,
            openlist_job_root=openlist_job_path,
            openlist_refresh_prefix=strm_refresh_prefix,
        )

        return {
            "version": STAGING_PLAN_VERSION,
            "enabled": True,
            "route": normalized_route,
            "category": str(category_key or "").strip(),
            "category_label": label,
            "job_id": int(job_id),
            "job_dir_name": f"job-{int(job_id)}",
            "staging_dir_name": self.directory_name,
            "provider_target_path": provider_target,
            "quark_source_category_root": quark_source_root,
            "quark_job_root": quark_job_path,
            "storage_backend": self._storage_backend(normalized_route),
            "storage_final_category_root": final_storage_root,
            "storage_staging_category_root": storage_staging_root,
            "storage_job_root": storage_job_path,
            "openlist_final_category_root": _as_openlist_path(final_openlist_root),
            "openlist_staging_category_root": _as_openlist_path(openlist_staging_root),
            "openlist_job_root": _as_openlist_path(openlist_job_path),
            # 固化本任务完成时应刷新的 OpenList STRM 分类目录，避免任务执行期间
            # 后台路径改动导致刷新到另一套挂载。空值会在完成阶段安全报错，绝不
            # 回退到 OpenList 根目录下的 /<影视名>。
            "openlist_refresh_prefix": _as_openlist_path(strm_refresh_prefix),
        }

    def _final_storage_root(self, route: str, category: dict[str, Any], label: str) -> str:
        if route == ROUTE_SIXPAN_OFFLINE:
            return _sixpan_final_storage_root(category, self.sixpan_config, label)
        if route == ROUTE_CLOUD139_DIRECT:
            return cmcc_upload_root(category, self.cloud139_config)
        backend = upload_backend(self.rclone_config, self.cmcc_upload_config)
        return rclone_upload_root(category, backend=backend, cloud139_config=self.cloud139_config)

    def _storage_backend(self, route: str) -> str:
        if route == ROUTE_CLOUD139_DIRECT:
            return "cmcc_api"
        if route == ROUTE_SIXPAN_OFFLINE:
            return "sixpan_offline"
        return upload_backend(self.rclone_config, self.cmcc_upload_config)

    def _final_openlist_root(self, route: str, category: dict[str, Any], label: str) -> str:
        if route == ROUTE_SIXPAN_OFFLINE:
            configured = str(category.get("sixpan_fnos_target_path") or "").strip().replace("\\", "/")
            if configured:
                return configured
            mount_name = str(
                self.sixpan_config.get("openlist_mount_name")
                or self.sixpan_config.get("fnos_mount_name")
                or self.sixpan_config.get("mount_name")
                or self.sixpan_config.get("mount_path")
                or "清云"
            ).strip().replace("\\", "/").strip("/")
            save_path = _sixpan_final_storage_root(category, self.sixpan_config, label).strip("/")
            return "/".join(part for part in (mount_name, save_path) if part)

        backend = "cmcc_api" if route == ROUTE_CLOUD139_DIRECT else upload_backend(self.rclone_config, self.cmcc_upload_config)
        return openlist_root_for_upload(category, backend=backend, cloud139_config=self.cloud139_config)

    def _strm_refresh_prefix(self, category_key: str) -> str:
        normalized = str(category_key or "").strip().lower()
        prefix = str(self.organizer_config.get(f"strm_refresh_prefix_{normalized}") or "").strip()
        if not prefix and normalized in {"anime", "variety"}:
            prefix = str(self.organizer_config.get("strm_refresh_prefix_tv") or "").strip()
        if not prefix:
            prefix = str(self.organizer_config.get("strm_refresh_prefix") or "").strip()
        return prefix

    @staticmethod
    def _reject_unsafe_paths(**paths: Any) -> None:
        unsafe = [key for key, value in paths.items() if value and _has_unsafe_path_segments(value)]
        if unsafe:
            raise ValueError(f"暂存路径包含不安全的 . 或 .. 路径段：{', '.join(unsafe)}")


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def _has_unsafe_path_segments(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    return any(part in {".", ".."} for part in text.split("/"))


def _as_openlist_path(value: Any) -> str:
    normalized = _normalize_path(value)
    return f"/{normalized}" if normalized else ""


def _sixpan_final_storage_root(
    category: dict[str, Any],
    sixpan_config: dict[str, Any],
    label: str,
) -> str:
    path = str(
        category.get("sixpan_save_path")
        or sixpan_config.get("default_save_path")
        or f"/{label}"
    ).strip().replace("\\", "/")
    legacy_prefix = "/离线下载/"
    if path.startswith(legacy_prefix):
        path = "/" + path[len(legacy_prefix) :].strip("/")
    return path
