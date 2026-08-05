from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from ..classifiers.link_classifier import detect_link
from ..constants import (
    COMPLETION_STAGE_FAILED,
    COMPLETION_STAGE_WAITING_ORGANIZER,
    COMPLETION_STAGE_WAITING_TRANSFER,
    EVENT_ERROR,
    EVENT_INFO,
    EVENT_WARN,
    JOB_DONE,
    JOB_PROVIDER_SUBMITTING,
    JOB_STATUS_LABELS,
    JOB_UNSUPPORTED,
    JOB_WAITING_ORGANIZER,
    ROUTE_CLOUD139_DIRECT,
    ROUTE_CLOUD189_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
    SOURCE_CLOUD139,
    SOURCE_CLOUD189,
    SOURCE_MAGNET,
    SOURCE_TORRENT,
)
from ..database import Database
from ..importers.cloud139 import Cloud139Importer
from ..importers.generic import GenericWebhookImporter
from ..importers.quark import QuarkImporter
from ..media.fnos import FnosMediaRefresher
from ..fnos_paths import match_actual_dirs, match_library, normalize_library_name, normalize_remote_hint, path_tails, split_values
from .import_job_service import (
    PROVIDER_SUBMISSION_FENCE_KEY,
    PROVIDER_SUBMISSION_FENCE_VERSION,
    ImportJobCreationService,
    ImportJobRetryService,
)
from .import_staging_service import ImportStagingService, staging_plan_from_job


class ImportService:
    def __init__(
        self,
        db: Database,
        config,
        quark_importer: QuarkImporter,
        fnos: FnosMediaRefresher,
        cloud139_importer: Cloud139Importer | None = None,
        generic_importers: dict[str, Any] | None = None,
    ):
        self.db = db
        self.config = config
        self.quark = quark_importer
        raw_config = getattr(config, "raw", {}) if config is not None else {}
        self.cloud139 = cloud139_importer or Cloud139Importer(raw_config.get("cloud139", {}), cmcc_config=raw_config.get("cmcc_upload", {}))
        self.fnos = fnos
        self.generic_importers = generic_importers or {}
        self.staging = ImportStagingService(config)
        self.job_creation = ImportJobCreationService(
            database=self.db,
            config=self.config,
            detect_link=detect_link,
            job_source_url=self._job_source_url,
            target_path=self._target_path,
            staging_plan=self.staging.build,
            submit_quark=self._submit_quark_job,
            submit_cloud139=self._submit_cloud139_job,
            submit_generic=self._submit_generic_job,
        )
        self.job_retry = ImportJobRetryService(
            database=self.db,
            config=self.config,
            submit_quark=self._submit_quark_job,
            submit_cloud139=self._submit_cloud139_job,
            submit_generic=self._submit_generic_job,
        )

    FNOS_DIR_REQUIRED_MESSAGE = "未匹配到飞牛媒体库真实刷新目录，已跳过扫描；请先在媒体库管理中确认分类 dir_list"

    def detect(self, url: str, password: str = "") -> dict[str, Any]:
        return detect_link(url, self.config.raw.get("routes", {}), password=password).to_dict()

    def create_import_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.job_creation.create(payload)

    def retry_job(self, job_id: int) -> dict[str, Any]:
        return self.job_retry.retry(job_id)

    def get_job_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        getter = getattr(self.db, "get_job_by_idempotency_key", None)
        if not callable(getter):
            return None
        return getter(idempotency_key)

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
            if library_info.get("guid"):
                return self.fnos.refresh_guid(str(library_info["guid"]), library=library, dir_list=dir_list)
            return self.fnos.refresh(library, dir_list=dir_list)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0\u5f02\u5e38\uff1a{exc}", "library": library, "dir_list": dir_list}

    def _submit_quark_job(
        self,
        job_id: int,
        title: str,
        url: str,
        target_path: str,
        category_key: str = "",
        category: dict[str, Any] | None = None,
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ready, blocked = self._provider_submission_ready(job_id)
        if not ready:
            return blocked
        self.db.add_event(job_id, EVENT_INFO, "提交 Quark 自动转存")
        request_payload = request_payload if isinstance(request_payload, dict) else {}
        result = self.quark.import_resource(
            title=title,
            share_url=url,
            save_path=target_path,
            category_key=category_key,
            category=category or {},
            quark_selection=self._extract_quark_selection(request_payload),
        )
        raw_data = self._merge_provider_raw_data(job_id, result.raw_data)
        if request_payload:
            raw_data = {**raw_data, "request": request_payload}
        # Quark/UC 只是先转存到任务级“离线下载”中转目录，后续还必须由
        # rclone 搬到移动云任务暂存目录；这里不能标记为等待 OpenList。
        stored_status = result.status
        completion = {
            "stage": COMPLETION_STAGE_WAITING_TRANSFER if result.success else COMPLETION_STAGE_FAILED,
            "official_save_path": result.target_path or target_path,
            "openlist_visible_path": "",
            "organizer_scan_path": "",
            "organized_target_path": "",
            "checks": [
                {
                    "name": "quark_submit",
                    "success": bool(result.success),
                    "message": "Quark 已转存到任务中转目录，等待 rclone 搬运到移动云任务暂存目录" if result.success else result.message,
                }
            ],
        }
        raw_data = {**raw_data, "completion": completion}
        persisted, stored_job, conflict_message = self._write_provider_result(
            job_id=job_id,
            provider="quark",
            status=stored_status,
            external_task_id=result.external_task_id,
            target_path=result.target_path or target_path,
            success=bool(result.success),
            message=result.message,
            raw_data=raw_data,
        )
        if not persisted:
            return self._provider_write_conflict_result(
                job=stored_job,
                message=conflict_message,
                provider_message=result.message,
                external_task_id=result.external_task_id,
            )
        self.db.add_event(job_id, EVENT_INFO if result.success else EVENT_ERROR, result.message, raw_data)
        return {
            "job": stored_job,
            "created": True,
            "message": result.message,
            "success": result.success,
            "status_label": JOB_STATUS_LABELS.get(stored_status, stored_status),
        }

    def _submit_cloud139_job(
        self,
        job_id: int,
        title: str,
        url: str,
        target_path: str,
        password: str,
        category: dict[str, Any],
        category_key: str = "",
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ready, blocked = self._provider_submission_ready(job_id)
        if not ready:
            return blocked
        self.db.add_event(job_id, EVENT_INFO, "提交 139 移动云官方直转")
        request_payload = request_payload if isinstance(request_payload, dict) else {}
        result = self.cloud139.import_resource(
            title=title,
            share_url=url,
            save_path=target_path,
            password=password,
            category_key=category_key,
            category=category,
            native_selection=self._extract_cloud139_native_selection(request_payload),
            target_root_is_resource=self._target_root_is_resource(request_payload),
        )
        raw_data = self._merge_provider_raw_data(job_id, result.raw_data)
        if request_payload:
            raw_data = {**raw_data, "request": request_payload}
        persisted, stored_job, conflict_message = self._write_provider_result(
            job_id=job_id,
            provider="cloud139",
            status=result.status,
            external_task_id=result.external_task_id,
            target_path=result.target_path or target_path,
            success=bool(result.success),
            message=result.message,
            raw_data=raw_data,
        )
        if not persisted:
            return self._provider_write_conflict_result(
                job=stored_job,
                message=conflict_message,
                provider_message=result.message,
                external_task_id=result.external_task_id,
            )
        self.db.add_event(job_id, EVENT_INFO if result.success else EVENT_ERROR, result.message, raw_data)
        directory_plan = result.raw_data.get("directory_plan") if isinstance(result.raw_data, dict) else None
        if isinstance(directory_plan, dict):
            self.db.add_event(
                job_id,
                EVENT_INFO,
                f"139 直转目录计划：{directory_plan.get('reason') or '已生成目录决策'}",
                directory_plan,
            )

        if result.success and getattr(self.cloud139, "refresh_after_submit", False):
            stored_job = self.db.get_job(job_id) or {}
            if staging_plan_from_job(stored_job) or self._defer_media_refresh_to_organizer():
                self.db.add_event(
                    job_id,
                    EVENT_INFO,
                    "任务级暂存整理已启用，139 直转提交阶段跳过飞牛刷新；Organizer 完成移动并触发 OpenList 文件夹刷新后即结束，随后按 Organizer 飞牛刷新开关异步处理，STRM 由 OpenList 后台生成。",
                    {
                        "route": ROUTE_CLOUD139_DIRECT,
                        "deferred_to_organizer": True,
                        "target_path": result.target_path or target_path,
                    },
                )
            else:
                extra_hints = self._cloud139_refresh_hints(category, result.target_path or target_path)
                self._record_media_refresh(
                    job_id,
                    category,
                    result.target_path or target_path,
                    resource_name=title,
                    trigger="immediate",
                    route=ROUTE_CLOUD139_DIRECT,
                    extra_hints=extra_hints,
                )
                self._schedule_delayed_media_refresh(
                    job_id,
                    category,
                    result.target_path or target_path,
                    title,
                    route=ROUTE_CLOUD139_DIRECT,
                    extra_hints=extra_hints,
                )

        return {
            "job": stored_job,
            "created": True,
            "message": result.message,
            "success": result.success,
            "status_label": JOB_STATUS_LABELS.get(result.status, result.status),
        }

    def _submit_generic_job(
        self,
        job_id: int,
        title: str,
        url: str,
        target_path: str,
        category: dict[str, Any],
        source_type: str,
        password: str = "",
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ready, blocked = self._provider_submission_ready(job_id)
        if not ready:
            return blocked
        importer = self._generic_importer(source_type)
        if not importer:
            message = f"{source_type} 线路已识别，但未配置对应入库接口"
            persisted, stored_job, conflict_message = self._write_provider_result(
                job_id=job_id,
                provider=source_type,
                status=JOB_UNSUPPORTED,
                external_task_id="",
                target_path=target_path,
                success=False,
                message=message,
                raw_data=self._job_raw_data(job_id),
            )
            if not persisted:
                return self._provider_write_conflict_result(
                    job=stored_job,
                    message=conflict_message,
                    provider_message=message,
                    external_task_id="",
                )
            self.db.add_event(job_id, EVENT_WARN, message)
            return {"job": stored_job, "created": True, "message": message, "success": False}

        is_sixpan = source_type in {SOURCE_MAGNET, SOURCE_TORRENT}
        self.db.add_event(job_id, EVENT_INFO, "提交六盘离线下载任务" if is_sixpan else f"提交 {source_type} 通用入库适配器")
        kwargs: dict[str, Any] = {}
        effective_request_payload = dict(request_payload) if isinstance(request_payload, dict) else {}
        if is_sixpan:
            ignore_files = self._extract_ignore_files(effective_request_payload)
            kwargs["ignore_files"] = ignore_files
            kwargs["save_path"] = target_path
            # Organizer 的六盘完整性检查从 request.ignore_files 读取固化选择。
            # 兼容只在 sixpan_selection 中传入忽略列表的调用方，避免把
            # 明确忽略的视频误算为应到达文件，导致任务永远无法完成。
            if ignore_files:
                effective_request_payload["ignore_files"] = ignore_files
        result = importer.import_resource(title=title, source_url=url, category=category, password=password, **kwargs)
        stored_status = JOB_WAITING_ORGANIZER if result.success and is_sixpan and result.status == JOB_DONE else result.status
        raw_data = self._merge_provider_raw_data(job_id, result.raw_data)
        if effective_request_payload:
            raw_data = {**raw_data, "request": effective_request_payload}
        if stored_status == JOB_WAITING_ORGANIZER:
            raw_data = {
                **raw_data,
                "completion": {
                    "stage": COMPLETION_STAGE_WAITING_ORGANIZER,
                    "official_save_path": result.target_path,
                    "message": "六盘离线任务已完成，等待 Organizer 标准化整理与标准目录确认",
                },
            }
        persisted, stored_job, conflict_message = self._write_provider_result(
            job_id=job_id,
            provider="sixpan" if is_sixpan else source_type,
            status=stored_status,
            external_task_id=result.external_task_id,
            target_path=result.target_path,
            success=bool(result.success),
            message=result.message,
            raw_data=raw_data,
        )
        if not persisted:
            return self._provider_write_conflict_result(
                job=stored_job,
                message=conflict_message,
                provider_message=result.message,
                external_task_id=result.external_task_id,
            )
        self.db.add_event(job_id, EVENT_INFO if result.success else EVENT_ERROR, result.message, raw_data)

        if result.success and is_sixpan and result.status == JOB_DONE:
            stored_job = self.db.get_job(job_id) or {}
            if staging_plan_from_job(stored_job) or self._defer_media_refresh_to_organizer():
                self.db.add_event(
                    job_id,
                    EVENT_INFO,
                    "OpenList 标准化已启用，六盘离线完成后跳过立即刷新，等待 Organizer 接管。",
                    {"route": ROUTE_SIXPAN_OFFLINE, "deferred_to_organizer": True, "target_path": result.target_path},
                )
            else:
                extra_hints = self._sixpan_refresh_hints(category, result.target_path)
                self._record_media_refresh(
                    job_id,
                    category,
                    result.target_path,
                    trigger="sixpan_done",
                    route=ROUTE_SIXPAN_OFFLINE,
                    extra_hints=extra_hints,
                )
        elif result.success and getattr(importer, "refresh_after_submit", False):
            self._record_media_refresh(job_id, category, result.target_path)

        return {
            "job": stored_job,
            "created": True,
            "message": result.message,
            "success": result.success,
            "status_label": JOB_STATUS_LABELS.get(stored_status, stored_status),
        }

    def _generic_importer(self, source_type: str) -> Any | None:
        if source_type == SOURCE_CLOUD139:
            return self.generic_importers.get("cloud139")
        if source_type == SOURCE_CLOUD189:
            return self.generic_importers.get("cloud189")
        if source_type in {SOURCE_MAGNET, SOURCE_TORRENT}:
            return self.generic_importers.get("sixpan")
        return None

    def _job_raw_data(self, job_id: int) -> dict[str, Any]:
        job = self.db.get_job(job_id) or {}
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        return dict(raw_data)

    def _merge_provider_raw_data(self, job_id: int, provider_raw_data: Any) -> dict[str, Any]:
        """Merge provider evidence without allowing it to replace the staging contract."""

        persisted = self._job_raw_data(job_id)
        provider = provider_raw_data if isinstance(provider_raw_data, dict) else {}
        merged = {**persisted, **provider}
        for key in ("staging_plan", "staging_plan_required"):
            if key in persisted:
                merged[key] = persisted[key]
            else:
                merged.pop(key, None)
        return merged

    def _provider_submission_ready(self, job_id: int) -> tuple[bool, dict[str, Any]]:
        job = self.db.get_job(job_id) or {}
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        fence = (
            raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY)
            if isinstance(raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY), dict)
            else {}
        )
        try:
            fence_version = int(fence.get("version") or 0)
        except (TypeError, ValueError):
            fence_version = 0
        ready = (
            str(job.get("status") or "").strip().lower() == JOB_PROVIDER_SUBMITTING
            and fence_version == PROVIDER_SUBMISSION_FENCE_VERSION
            and str(fence.get("state") or "").strip().lower() == "submitting"
        )
        if ready:
            return True, {}
        message = "网盘提交原子栅栏未建立或状态已变化，已停止调用外部接口"
        if job.get("id"):
            self.db.add_event(
                job_id,
                EVENT_WARN,
                message,
                {"status": job.get("status"), "provider_submission_fence": fence},
            )
        return False, {
            "job": job,
            "created": False,
            "message": message,
            "success": False,
            "provider_submission_claim_failed": True,
            "retryable": True,
        }

    def _write_provider_result(
        self,
        *,
        job_id: int,
        provider: str,
        status: str,
        external_task_id: Any,
        target_path: str,
        success: bool,
        message: str,
        raw_data: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str]:
        current = self.db.get_job(job_id) or {}
        persisted_raw = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
        incoming_raw = raw_data if isinstance(raw_data, dict) else {}
        fence = (
            persisted_raw.get(PROVIDER_SUBMISSION_FENCE_KEY)
            if isinstance(persisted_raw.get(PROVIDER_SUBMISSION_FENCE_KEY), dict)
            else {}
        )
        finished_fence = {
            **fence,
            "version": PROVIDER_SUBMISSION_FENCE_VERSION,
            "state": "finished",
            "provider": str(provider or fence.get("provider") or "unknown"),
            "success": bool(success),
            "result_status": str(status or ""),
            "finished_at": _utc_now_text(),
        }
        completed_raw = {
            **persisted_raw,
            **incoming_raw,
            PROVIDER_SUBMISSION_FENCE_KEY: finished_fence,
        }
        updater = getattr(self.db, "update_job_if_status", None)
        if not callable(updater):
            latest = self.db.get_job(job_id) or current
            return False, latest, "任务存储不支持原子结果写回，网盘结果未覆盖本地状态"
        changed = bool(
            updater(
                job_id,
                {JOB_PROVIDER_SUBMITTING},
                status=status,
                external_task_id=external_task_id,
                target_path=target_path,
                error_message="" if success else message,
                raw_data=completed_raw,
            )
        )
        latest = self.db.get_job(job_id) or current
        if changed:
            return True, latest, ""
        conflict_message = (
            "网盘接口已返回，但任务状态已并发变化；结果未覆盖本地状态。"
            "请核对外部任务与本地任务后人工处理"
        )
        self.db.add_event(
            job_id,
            EVENT_WARN,
            conflict_message,
            {
                "provider": provider,
                "provider_status": status,
                "provider_success": bool(success),
                "external_task_id": external_task_id,
                "current_status": latest.get("status"),
            },
        )
        return False, latest, conflict_message

    @staticmethod
    def _provider_write_conflict_result(
        *,
        job: dict[str, Any],
        message: str,
        provider_message: str,
        external_task_id: Any,
    ) -> dict[str, Any]:
        return {
            "job": job,
            "created": False,
            "message": message,
            "provider_message": provider_message,
            "external_task_id": external_task_id,
            "success": False,
            "provider_write_conflict": True,
            "manual_review": True,
        }

    @staticmethod
    def _job_source_url(url: str, payload: dict[str, Any]) -> str:
        scoped = {
            "quark_selection": payload.get("quark_selection") if isinstance(payload.get("quark_selection"), dict) else {},
            "cloud139_selection": payload.get("cloud139_selection") if isinstance(payload.get("cloud139_selection"), dict) else {},
        }
        # SixPan's effective selection is represented by the ignored file
        # identities.  Keep only this semantic field in the job identity:
        # transient preview metadata (parse status/counts) must not create a
        # different job for the same set of files.
        sixpan_ignore_files = ImportService._extract_ignore_files(payload)
        if sixpan_ignore_files:
            scoped["sixpan_selection"] = {"ignore_files": sorted(set(sixpan_ignore_files))}
        if not scoped["quark_selection"] and not scoped["cloud139_selection"]:
            if "sixpan_selection" not in scoped:
                return url
        try:
            text = json.dumps(scoped, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            return url
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
        sep = "&" if "#" in url else "#"
        return f"{url}{sep}fnos_selection={digest}"

    @staticmethod
    def _extract_ignore_files(payload: dict[str, Any]) -> list[str]:
        value = payload.get("ignore_files") if isinstance(payload, dict) else None
        if value is None and isinstance(payload, dict):
            selection = payload.get("sixpan_selection")
            if isinstance(selection, dict):
                value = selection.get("ignore_files")
        if value is None:
            return []
        raw = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text and text not in result and len(text) <= 512:
                result.append(text)
        return result

    @staticmethod
    def _extract_quark_selection(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("quark_selection") if isinstance(payload, dict) else None
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _extract_cloud139_native_selection(payload: dict[str, Any]) -> dict[str, Any] | None:
        selection = payload.get("cloud139_selection") if isinstance(payload, dict) else None
        if not isinstance(selection, dict):
            return None

        def _clean_rows(value: Any, *, is_dir: bool, max_items: int) -> list[dict[str, Any]]:
            rows = value if isinstance(value, list) else []
            result: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in rows[:max_items]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("fileName") or "").strip()[:240]
                path = str(item.get("path") or "").strip()[:1024]
                token = str(item.get("share_fid_token") or "").strip()[:2048]
                fid = str(item.get("fid") or item.get("id") or "").strip()[:256]
                key = path or token or fid
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append({"name": name, "path": path, "share_fid_token": token, "fid": fid, "type": "dir" if is_dir else "file", "is_dir": is_dir})
            return result

        files = _clean_rows(selection.get("selected_files"), is_dir=False, max_items=300)
        folders = _clean_rows(selection.get("selected_folders"), is_dir=True, max_items=100)
        return {"selected_files": files, "selected_folders": folders} if files or folders else None

    def sixpan_refresh_hints(self, category: dict[str, Any], target_path: str = "") -> list[str]:
        return self._sixpan_refresh_hints(category, target_path)

    def _defer_media_refresh_to_organizer(self) -> bool:
        raw = getattr(self.config, "raw", {}) if self.config else {}
        organizer = raw.get("organizer", {}) if isinstance(raw.get("organizer"), dict) else {}
        openlist = raw.get("openlist", {}) if isinstance(raw.get("openlist"), dict) else {}
        return bool(organizer.get("enabled", False) and str(openlist.get("base_url") or "").strip())

    def record_sixpan_completed(self, job_id: int, category: dict[str, Any], target_path: str = "", trigger: str = "sixpan_done") -> dict[str, Any]:
        return self._record_media_refresh(
            job_id,
            category,
            target_path,
            trigger=trigger,
            route=ROUTE_SIXPAN_OFFLINE,
            extra_hints=self._sixpan_refresh_hints(category, target_path),
        )

    def _sixpan_refresh_hints(self, category: dict[str, Any], target_path: str = "") -> list[str]:
        label = str(category.get("label") or "").strip()
        save_path = self._normalize_remote_hint(target_path or category.get("sixpan_save_path") or label).strip("/")
        suffixes = [item for item in [save_path, label] if item]
        values: list[Any] = [
            category.get("sixpan_fnos_dir_list"),
            category.get("sixpan_fnos_target_path"),
            category.get("sixpan_mount_path"),
            target_path,
        ]
        importer = self.generic_importers.get("sixpan")
        configured_mounts = [
            getattr(importer, "fnos_mount_name", "") if importer else "",
            getattr(importer, "mount_name", "") if importer else "",
            category.get("sixpan_mount_path"),
        ]
        raw_config = getattr(importer, "config", {}) if importer else {}
        if isinstance(raw_config, dict):
            configured_mounts.extend([raw_config.get("fnos_mount_name"), raw_config.get("mount_name"), raw_config.get("mount_path")])
        mount_names = [*configured_mounts, "六盘", "6盘"]
        for mount in mount_names:
            mount_text = self._normalize_remote_hint(mount).strip("/")
            if not mount_text:
                continue
            for suffix in suffixes:
                suffix_text = self._normalize_remote_hint(suffix).strip("/")
                if suffix_text and not suffix_text.startswith(f"{mount_text}/"):
                    values.append(f"{mount_text}/{suffix_text}")
        result: list[str] = []
        for value in values:
            for item in self._split_fnos_values(value):
                normalized = self._normalize_remote_hint(item)
                if normalized and normalized not in result:
                    result.append(normalized)
        return result


    def _cloud139_refresh_hints(self, category: dict[str, Any], target_path: str = "") -> list[str]:
        values = [
            category.get("cloud139_fnos_dir_list"),
            category.get("cloud139_fnos_target_path"),
            category.get("cloud139_mount_path"),
        ]
        mount_name = "" if category.get("cloud139_fnos_target_path") else str(getattr(self.cloud139, "fnos_mount_name", "") or "").strip()
        if mount_name:
            for suffix in self._cloud139_refresh_suffixes(category, target_path):
                values.append(f"{mount_name}/{suffix}")
        result: list[str] = []
        for value in values:
            for item in self._split_fnos_values(value):
                normalized = self._normalize_remote_hint(item)
                if normalized and normalized not in result:
                    result.append(normalized)
        return result

    def _cloud139_refresh_suffixes(self, category: dict[str, Any], target_path: str = "") -> list[str]:
        """Return the path below the FNOS/OpenList mount for a 139 direct save.

        139 cloud save paths and FNOS WebDAV mount paths are two different
        namespaces.  Example:
        - 139 cloud path: 博客/电影
        - FNOS mount:     移动云2/电影

        With target_root_path=博客 and fnos_mount_name=移动云2, the suffix
        used for FNOS matching must be only "电影".
        """

        root = self._normalize_remote_hint(getattr(self.cloud139, "target_root_path", "")).strip("/")
        raw_values = [
            category.get("cloud139_fnos_sub_path"),
            category.get("cloud139_target_sub_path"),
            category.get("cloud139_target_path"),
            target_path,
            category.get("label"),
        ]
        result: list[str] = []
        for raw in raw_values:
            for item in self._split_fnos_values(raw):
                text = self._normalize_remote_hint(item).strip("/")
                if not text:
                    continue
                stripped = text
                if root and (text == root or text.startswith(f"{root}/")):
                    stripped = text[len(root) :].strip("/")
                if stripped and stripped not in result:
                    result.append(stripped)
                if not root or stripped == text:
                    for tail in self._path_tails(text)[1:]:
                        if tail and tail not in result:
                            result.append(tail)
        return result

    def _schedule_delayed_media_refresh(
        self,
        job_id: int,
        category: dict[str, Any],
        target_path: str,
        resource_name: str = "",
        route: str = "",
        extra_hints: list[str] | None = None,
    ) -> None:
        delay_seconds = int(getattr(self.cloud139, "refresh_delay_seconds", 90) or 0)
        if delay_seconds <= 0:
            return
        payload = {
            "delay_seconds": delay_seconds,
            "target_path": target_path,
            "resource_name": resource_name,
            "reason": "移动云 WebDAV 挂载可能存在可见性延迟，延迟二次扫描避免飞牛扫到空目录",
        }
        self.db.add_event(job_id, EVENT_INFO, f"已安排飞牛延迟刷新：{delay_seconds} 秒后执行", payload)

        def _run() -> None:
            try:
                self._record_media_refresh(
                    job_id,
                    category,
                    target_path,
                    resource_name=resource_name,
                    trigger="delayed",
                    route=route,
                    extra_hints=extra_hints,
                )
            except Exception as exc:  # noqa: BLE001
                self.db.add_event(job_id, EVENT_WARN, f"飞牛延迟刷新异常：{exc}", payload)

        timer = threading.Timer(delay_seconds, _run)
        timer.daemon = True
        timer.start()

    def _record_media_refresh(
        self,
        job_id: int,
        category: dict[str, Any],
        target_path: str = "",
        resource_name: str = "",
        trigger: str = "manual",
        route: str = "",
        extra_hints: list[str] | None = None,
    ) -> dict[str, Any]:
        library_info = self._fnos_library_info(category, target_path=target_path, route=route, extra_hints=extra_hints)
        library = library_info["library"]
        dir_list = library_info["dir_list"]
        base_dir_list = list(dir_list)
        if resource_name:
            dir_list = self._resource_refresh_dirs(dir_list, resource_name)
        if not dir_list:
            refresh_result = {
                "success": False,
                "message": self.FNOS_DIR_REQUIRED_MESSAGE,
                "library": library,
                "dir_list": [],
                "target_hints": library_info.get("target_hints") or [],
                "target_path": target_path,
                "resource_name": resource_name,
                "trigger": trigger,
                "skipped": True,
            }
            self.db.add_event(job_id, EVENT_WARN, refresh_result["message"], refresh_result)
            return refresh_result
        refresh_request = {
            "method": "POST",
            "api": f"/v/api/v1/mdb/scan/{library_info.get('guid')}" if library_info.get("guid") else "/v/api/v1/mdb/scan/{guid}",
            "library": library,
            "guid": str(library_info.get("guid") or ""),
            "body": {"dir_list": dir_list},
            "dir_list": dir_list,
            "base_dir_list": base_dir_list,
            "target_path": target_path,
            "resource_name": resource_name,
            "target_hints": library_info.get("target_hints") or [],
            "trigger": trigger,
            "route": route,
        }
        self.db.add_event(job_id, EVENT_INFO, "飞牛媒体库刷新请求参数", refresh_request)
        try:
            if library_info.get("guid"):
                refresh_result = self.fnos.refresh_guid(str(library_info["guid"]), library=library, dir_list=dir_list)
            else:
                refresh_result = self.fnos.refresh(library, dir_list=dir_list)
        except Exception as exc:  # noqa: BLE001
            refresh_result = {"success": False, "message": f"\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0\u5f02\u5e38\uff1a{exc}", "library": library, "dir_list": dir_list}
        if refresh_result.get("success"):
            self.db.add_event(job_id, EVENT_INFO, "\u5df2\u89e6\u53d1\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0", refresh_result)
        else:
            self.db.add_event(
                job_id,
                EVENT_WARN,
                refresh_result.get("message") or "\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0\u5931\u8d25",
                refresh_result,
            )
        return refresh_result

    @classmethod
    def _resource_refresh_dirs(cls, base_dirs: list[str], resource_name: str) -> list[str]:
        child = cls._normalize_remote_hint(resource_name).strip("/")
        if not child:
            return base_dirs
        result: list[str] = []
        for base in base_dirs:
            base_norm = cls._normalize_remote_hint(base)
            if not base_norm.startswith("/"):
                continue
            candidate = f"{base_norm.rstrip('/')}/{child}"
            if candidate not in result:
                result.append(candidate)
        return result or base_dirs

    def _fnos_library_info(self, category: dict[str, Any], target_path: str = "", route: str = "", extra_hints: list[str] | None = None) -> dict[str, Any]:
        library = str(category.get("fnos_lib") or category.get("label") or "").strip()
        fallback_dirs = self._fnos_dir_list(category, target_path=target_path, route=route)
        target_hints = self._fnos_target_hints(category, target_path=target_path, route=route, extra_hints=extra_hints)
        try:
            result = self.fnos.refresh_libraries()
        except Exception:  # noqa: BLE001
            result = {}
        items = result.get("items") if isinstance(result, dict) and isinstance(result.get("items"), list) else []
        matched = self._match_fnos_library(items, library)
        if matched:
            actual_dirs = self._match_fnos_target_dirs(matched.get("dir_list"), target_hints)
            return {
                "library": str(matched.get("name") or matched.get("title") or library),
                "guid": str(matched.get("guid") or ""),
                "dir_list": actual_dirs or fallback_dirs,
                "target_hints": target_hints,
            }
        return {"library": library, "guid": "", "dir_list": fallback_dirs, "target_hints": target_hints}

    @classmethod
    def _match_fnos_library(cls, items: list[Any], library: str) -> dict[str, Any] | None:
        return match_library(items, library)

    @staticmethod
    def _normalize_fnos_library_name(value: Any) -> str:
        return normalize_library_name(value)

    @classmethod
    def _fnos_target_hints(cls, category: dict[str, Any], target_path: str = "", route: str = "", extra_hints: list[str] | None = None) -> list[str]:
        if route == ROUTE_CLOUD139_DIRECT:
            values = [
                *(extra_hints or []),
                category.get("cloud139_fnos_dir_list"),
                category.get("cloud139_fnos_target_path"),
                category.get("cloud139_mount_path"),
                category.get("cloud139_target_path"),
                target_path,
                category.get("label"),
            ]
        elif route == ROUTE_SIXPAN_OFFLINE:
            values = [
                *(extra_hints or []),
                category.get("sixpan_fnos_dir_list"),
                category.get("sixpan_fnos_target_path"),
                category.get("sixpan_mount_path"),
                category.get("sixpan_save_path"),
                target_path,
                category.get("label"),
            ]
        else:
            values = [
                target_path,
                category.get("mobile_target_path"),
                category.get("fnos_target_path"),
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
    def _match_fnos_target_dirs(cls, actual_value: Any, target_hints: list[str]) -> list[str]:
        actual_dirs = cls._absolute_fnos_dirs(actual_value)
        if not actual_dirs or not target_hints:
            return []
        for hint in target_hints:
            matches = cls._match_actual_fnos_dirs(actual_dirs, hint)
            if matches and (cls._hint_segment_count(hint) >= 2 or len(matches) == 1 or hint.startswith("/")):
                return matches
        return []

    @classmethod
    def _match_actual_fnos_dirs(cls, actual_dirs: list[str], hint: str) -> list[str]:
        return match_actual_dirs(actual_dirs, hint)

    @staticmethod
    def _path_tails(path: str) -> list[str]:
        return path_tails(path)

    @classmethod
    def _hint_segment_count(cls, value: str) -> int:
        return len([part for part in cls._normalize_remote_hint(value).strip("/").split("/") if part])

    @staticmethod
    def _normalize_remote_hint(value: Any) -> str:
        return normalize_remote_hint(value)

    @staticmethod
    def _split_fnos_values(value: Any) -> list[str]:
        return split_values(value)

    @staticmethod
    def _fnos_dir_list(category: dict[str, Any], target_path: str = "", route: str = "") -> list[str]:
        if route == ROUTE_CLOUD139_DIRECT:
            configured = category.get("cloud139_fnos_dir_list") or category.get("cloud139_fnos_dirs")
        elif route == ROUTE_SIXPAN_OFFLINE:
            configured = (
                category.get("sixpan_fnos_dir_list")
                or category.get("sixpan_fnos_dirs")
                or category.get("fnos_dir_list")
                or category.get("fnos_dirs")
                or category.get("fnos_mdb_dir_list")
            )
        else:
            configured = category.get("fnos_dir_list") or category.get("fnos_dirs") or category.get("fnos_mdb_dir_list")
        values: list[Any]
        if isinstance(configured, str):
            values = configured.replace("\n", ",").replace("|", ",").split(",")
        elif isinstance(configured, (list, tuple, set)):
            values = list(configured)
        else:
            values = []

        target = str(target_path or "").strip()
        if target.startswith("/") and route != ROUTE_SIXPAN_OFFLINE:
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
            if not text or not text.startswith("/"):
                continue
            normalized = ImportService._normalize_remote_hint(text)
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def _target_path(self, route: str, category: dict[str, Any], request_payload: dict[str, Any] | None = None) -> str:
        request_payload = request_payload if isinstance(request_payload, dict) else {}
        if route == ROUTE_QUARK_TO_MOBILE:
            return str(category.get("quark_save_path") or "")
        if route == ROUTE_CLOUD139_DIRECT:
            override = self._target_path_override(request_payload)
            if override:
                return override
            resolver = getattr(self.cloud139, "target_path_for_category", None)
            if callable(resolver):
                return str(resolver(category=category) or "")
            return str(category.get("cloud139_target_path") or category.get("mobile_target_path") or category.get("quark_save_path") or "")
        if route == ROUTE_SIXPAN_OFFLINE:
            importer = self.generic_importers.get("sixpan")
            resolver = getattr(importer, "target_path_for_category", None)
            if callable(resolver):
                return str(resolver(category) or "")
            return str(category.get("sixpan_save_path") or category.get("quark_save_path") or "")
        return str(category.get("mobile_target_path") or category.get("quark_save_path") or "")

    @staticmethod
    def _target_path_override(payload: dict[str, Any]) -> str:
        context = payload.get("update_context") if isinstance(payload.get("update_context"), dict) else {}
        values = [
            payload.get("target_path_override"),
            payload.get("cloud139_target_path_override"),
            context.get("cloud139_target_path"),
            context.get("target_path_override"),
        ]
        for value in values:
            text = str(value or "").strip().replace("\\", "/")
            if not text or "://" in text:
                continue
            lowered = text.strip("/").lower()
            if lowered in {"none", "null", "undefined"}:
                continue
            return text.strip("/")
        return ""

    @staticmethod
    def _target_root_is_resource(payload: dict[str, Any]) -> bool:
        context = payload.get("update_context") if isinstance(payload.get("update_context"), dict) else {}
        organizer_context = payload.get("organizer_context") if isinstance(payload.get("organizer_context"), dict) else {}
        values = [
            payload.get("target_root_is_resource"),
            context.get("target_root_is_resource"),
            organizer_context.get("target_root_is_resource"),
        ]
        return any(str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是"} or value is True for value in values)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
