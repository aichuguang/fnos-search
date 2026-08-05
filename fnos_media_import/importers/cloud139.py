from __future__ import annotations

import json
import os
import time
from typing import Any

from ..cmcc.auth import CmccAuthConfigError, CmccAuthProvider
from ..cmcc.client import CmccApiError, CmccClient
from ..constants import JOB_FAILED, JOB_SUBMITTED
from ..media_path_rules import append_resource_dir, sanitize_resource_dir_name
from .base import AdapterCheckResult, ImportResult


class Cloud139Importer:
    """139 移动云官方直转适配器。

    分享预览、目录展开、单文件/文件夹转存都复用项目现有的
    cmcc_upload 官方 API 认证，不需要额外转存服务。
    """

    def __init__(self, config: dict[str, Any], cmcc_config: dict[str, Any] | None = None):
        self.config = config if isinstance(config, dict) else {}
        self.cmcc_config = cmcc_config if isinstance(cmcc_config, dict) else {}
        self.timeout = int(self.config.get("timeout", self.cmcc_config.get("timeout", 30)) or 30)
        self.target_root_path = _normalize_cloud_path(
            self.config.get("target_root_path")
            or self.config.get("cloud_root_path")
            or self.config.get("save_root_path")
            or self.config.get("root_path")
            or ""
        )
        self.fnos_mount_name = str(self.config.get("fnos_mount_name") or self.config.get("mount_name") or "").strip()
        self.check_before_save = _as_bool(self.config.get("check_before_save"), True)
        self.refresh_after_submit = _as_bool(self.config.get("refresh_after_submit"), True)
        self.refresh_delay_seconds = max(0, int(self.config.get("refresh_delay_seconds", 90) or 0))
        self.mark_done_after_submit = _as_bool(self.config.get("mark_done_after_submit"), True)
        self.create_folder_if_missing = _as_bool(self.config.get("create_folder_if_missing"), True)
        self.default_target_path = _normalize_cloud_path(self.config.get("target_path") or self.config.get("target_folder") or "")
        self.category_target_paths = _dict_of_str(self.config.get("category_target_paths"))
        self._cmcc_client: CmccClient | None = None

    @property
    def configured(self) -> bool:
        if self.cmcc_config.get("enabled") is False:
            return False
        if str(self.cmcc_config.get("access_token") or self.cmcc_config.get("authorization") or "").strip():
            return True
        return bool(
            os.getenv("CMCC_ACCESS_TOKEN")
            or os.getenv("CMCC_AUTHORIZATION")
            or os.getenv("CMCC_BASIC_AUTH")
            or os.getenv("CM_CLOUD_ACCESS_TOKEN")
        )

    @property
    def native_configured(self) -> bool:
        return self.configured

    def describe(self) -> dict[str, Any]:
        return {
            "name": "139 移动云官方直转",
            "adapter_type": "cloud139_cmcc_native",
            "configured": self.configured,
            "capabilities": {
                "submit": self.configured,
                "share_check": self.configured,
                "file_list": self.configured,
                "native_submit": self.configured,
                "native_file_list": self.configured,
                "direct_refresh": self.refresh_after_submit,
                "mark_done_after_submit": self.mark_done_after_submit,
            },
            "config": {
                "cmcc_upload_enabled": self.cmcc_config.get("enabled", True) is not False,
                "cmcc_access_token_configured": bool(str(self.cmcc_config.get("access_token") or self.cmcc_config.get("authorization") or "").strip()),
                "cmcc_phone_configured": bool(str(self.cmcc_config.get("phone") or "").strip()),
                "target_root_path": self.target_root_path,
                "fnos_mount_name": self.fnos_mount_name,
                "refresh_delay_seconds": self.refresh_delay_seconds,
                "timeout": self.timeout,
            },
        }

    def check_configuration(self) -> AdapterCheckResult:
        if not self.configured:
            return AdapterCheckResult(False, "unconfigured", "139 官方直转未配置：请在后台“移动云官方上传”中维护 CMCC Basic 认证", self.describe())
        return AdapterCheckResult(True, "ready", "139 官方直转适配器已配置", self.describe())

    def probe(self) -> AdapterCheckResult:
        config_result = self.check_configuration()
        if not config_result.ok:
            return config_result
        details = self.describe()
        try:
            client = self._cmcc()
            details["phone_configured"] = bool(client.phone)
        except Exception as exc:  # noqa: BLE001
            return AdapterCheckResult(False, "probe_failed", f"139 官方直转认证检测失败：{exc}", details)
        return AdapterCheckResult(True, "ready", "139 官方直转认证检测通过", details)

    def check_share(self, share_url: str, title: str = "", password: str = "", fid: str = "") -> tuple[bool, dict[str, Any]]:
        if not str(share_url or "").strip():
            return False, {"success": False, "message": "139 移动云分享链接不能为空", "provider": "cmcc_native"}
        if not self.configured:
            return False, {"success": False, "message": self.check_configuration().message, "provider": "cmcc_native"}
        try:
            link_id, parsed_password, pdir_fid = CmccClient.extract_share_url(share_url)
            pdir = _normalize_share_fid(fid) or pdir_fid or "root"
            passcode = password or parsed_password
            items = self._cmcc().list_share_dir(link_id, password=passcode, pdir_fid=pdir)
        except (CmccApiError, CmccAuthConfigError, RuntimeError) as exc:
            return False, {"success": False, "message": f"139 官方分享检测失败：{exc}", "provider": "cmcc_native"}
        share_title = title or "139 分享"
        normalized = [_normalize_share_item(item) for item in items]
        return True, {
            "success": True,
            "message": "139 官方分享检测通过",
            "provider": "cmcc_native",
            "data": {
                "share": {
                    "title": share_title,
                    "name": share_title,
                    "status": "valid",
                    "file_count": len(normalized),
                    "link_id": link_id,
                    "pdir_fid": pdir,
                },
                "items": normalized,
            },
        }

    def list_files(self, share_url: str, fid: str, password: str = "", title: str = "") -> dict[str, Any]:
        if not self.configured:
            return {"success": False, "message": self.check_configuration().message, "items": [], "provider": "cmcc_native"}
        try:
            link_id, parsed_password, pdir_fid = CmccClient.extract_share_url(share_url)
            pdir = _normalize_share_fid(fid) or pdir_fid or "root"
            items = [_normalize_share_item(item) for item in self._cmcc().list_share_dir(link_id, password=password or parsed_password, pdir_fid=pdir)]
        except (CmccApiError, CmccAuthConfigError, RuntimeError) as exc:
            return {"success": False, "message": f"139 官方目录读取失败：{exc}", "items": [], "provider": "cmcc_native"}
        return {
            "success": True,
            "message": "139 官方目录读取成功",
            "provider": "cmcc_native",
            "items": items,
            "data": {"items": items, "share": {"title": title or "139 分享", "pdir_fid": pdir}},
        }

    def import_resource(
        self,
        title: str,
        share_url: str,
        save_path: str,
        password: str = "",
        pattern: str = r"(?:【.*?】)?(.*)",
        replace: str = r"\1",
        category_key: str = "",
        category: dict[str, Any] | None = None,
        selected_folders: list[str] | None = None,
        native_selection: dict[str, Any] | None = None,
        target_root_is_resource: bool = False,
    ) -> ImportResult:
        del pattern, replace
        category = category or {}
        if not self.configured:
            return ImportResult(False, JOB_FAILED, self.check_configuration().message, target_path=save_path)

        native_selection = native_selection if isinstance(native_selection, dict) else {}
        files = _normalize_selected_files(native_selection.get("selected_files"))
        folders = _normalize_selected_folders(native_selection.get("selected_folders"))
        if not folders and selected_folders:
            folders = _normalize_selected_folders(selected_folders)

        default_full_share = False
        default_root_items: list[dict[str, Any]] = []
        try:
            link_id, parsed_password, pdir_fid = CmccClient.extract_share_url(share_url)
            passcode = password or parsed_password
            content_ids = [_selected_item_save_value(item) for item in files]
            catalog_ids = [_selected_item_save_value(item) for item in folders]
            content_ids = [item for item in content_ids if item]
            catalog_ids = [item for item in catalog_ids if item]
            if not content_ids and not catalog_ids:
                default_full_share = True
                default_root_items = [_normalize_share_item(item) for item in self._cmcc().list_share_dir(link_id, password=passcode, pdir_fid=pdir_fid or "root")]
                files = [item for item in default_root_items if not item.get("is_dir")]
                folders = [item for item in default_root_items if item.get("is_dir")]
                content_ids = [_selected_item_save_value(item) for item in files]
                catalog_ids = [_selected_item_save_value(item) for item in folders]
                content_ids = [item for item in content_ids if item]
                catalog_ids = [item for item in catalog_ids if item]
                if not content_ids and not catalog_ids:
                    return ImportResult(
                        False,
                        JOB_FAILED,
                        "139 官方直转默认保存整个分享失败：分享目录为空或未返回可保存条目",
                        target_path=save_path,
                        raw_data={"selection": native_selection or {}, "default_full_share": True, "default_root_items": default_root_items},
                    )

            directory_plan = self._build_directory_plan(
                title=title,
                save_path=save_path,
                category_key=category_key,
                category=category,
                files=files,
                folders=folders,
                default_full_share=default_full_share,
                target_root_is_resource=target_root_is_resource,
            )
            target_id = self._resolve_target_parent_id(str(directory_plan.get("target_path") or ""), category_key=category_key, category=category)
            save_result = self._cmcc().save_share_items(
                link_id=link_id,
                target_parent_file_id=target_id,
                content_paths=content_ids,
                catalog_paths=catalog_ids,
                need_password=bool(passcode),
                password=passcode,
            )
        except (CmccApiError, CmccAuthConfigError, RuntimeError) as exc:
            return ImportResult(
                False,
                JOB_FAILED,
                f"139 官方直转失败：{exc}",
                target_path=save_path,
                raw_data={"selection": {"files": files, "folders": folders}, "default_full_share": default_full_share, "error": str(exc)},
            )

        task_res = save_result.get("createOuterLinkBatchOprTaskRes") if isinstance(save_result.get("createOuterLinkBatchOprTaskRes"), dict) else {}
        task_id = _first_non_empty(save_result.get("taskID"), save_result.get("taskId"), task_res.get("taskID"), task_res.get("taskId"))
        status = self._native_status(task_id)
        raw_data = {
            "provider": "cmcc_native",
            "directory_plan": directory_plan,
            "selection": {
                "selected_files": files,
                "selected_folders": folders,
                "content_ids": content_ids,
                "catalog_ids": catalog_ids,
                "default_full_share": default_full_share,
                "default_root_item_count": len(default_root_items),
            },
            "save": save_result,
            "target_parent_file_id": target_id,
        }
        target_path_text = str(directory_plan.get("target_path") or save_path)
        action_text = "139 官方整分享直转" if default_full_share else "139 官方文件级直转"
        message = f"{action_text}已提交，保存位置：{target_path_text}"
        if status == JOB_FAILED:
            return ImportResult(
                False,
                JOB_FAILED,
                "139 官方文件级直转任务已提交但任务状态返回失败",
                external_task_id=str(task_id or ""),
                target_path=target_path_text,
                raw_data=raw_data,
            )
        return ImportResult(
            True,
            status,
            message,
            external_task_id=str(task_id or ""),
            target_path=target_path_text,
            raw_data=raw_data,
        )

    def target_path_for_category(
        self,
        save_path: str = "",
        category_key: str = "",
        category: dict[str, Any] | None = None,
    ) -> str:
        category = category or {}
        category_path = _normalize_cloud_path(
            category.get("cloud139_target_path")
            or self.category_target_paths.get(category_key)
        )
        if category_path:
            return self._with_target_root(category_path)
        category_sub_path = _normalize_cloud_path(category.get("cloud139_target_sub_path"))
        if category_sub_path:
            return self._with_target_root(category_sub_path)
        fallback_path = _normalize_cloud_path(save_path or self.default_target_path or category.get("label") or category_key)
        return fallback_path

    def _cmcc(self) -> CmccClient:
        if self._cmcc_client is None:
            self._cmcc_client = CmccClient(CmccAuthProvider(self.cmcc_config), timeout=self.timeout)
        return self._cmcc_client

    def _build_directory_plan(
        self,
        *,
        title: str,
        save_path: str,
        category_key: str,
        category: dict[str, Any],
        files: list[dict[str, Any]],
        folders: list[dict[str, Any]],
        default_full_share: bool = False,
        target_root_is_resource: bool = False,
    ) -> dict[str, Any]:
        base_target_path = _normalize_cloud_path(save_path) if str(save_path or "").strip() else self.target_path_for_category(category_key=category_key, category=category)
        resource_name = sanitize_resource_dir_name(title or category.get("label") or category_key or "未命名资源")
        target_path = base_target_path if target_root_is_resource else (append_resource_dir(base_target_path, resource_name) if resource_name else base_target_path)
        strategy = "cmcc_native_full_share" if default_full_share else "cmcc_native_selected_items"
        if target_root_is_resource:
            reason = "139 官方直转按追更识别的既有资源根保存，不再追加剧名目录"
        else:
            reason = "139 官方直转未勾选条目，默认保存整个分享" if default_full_share else "139 官方直转按已勾选文件/文件夹保存"
        return {
            "category": category_key,
            "resource_name": resource_name,
            "base_target_path": base_target_path,
            "target_path": target_path,
            "resource_dir_applied": target_path != base_target_path,
            "target_root_is_resource": bool(target_root_is_resource),
            "strategy": strategy,
            "reason": reason,
            "selected_file_count": len(files),
            "selected_folder_count": len(folders),
            "default_full_share": default_full_share,
        }

    def _resolve_target_parent_id(self, target_path: str, *, category_key: str = "", category: dict[str, Any]) -> str:
        explicit_id = str(category.get("cloud139_folder_id") or category.get("cloud139_target_folder_id") or category.get("cmcc_parent_file_id") or "").strip()
        if explicit_id and _same_cloud_path(target_path, self.target_path_for_category(category_key=category_key, category=category)):
            return "/" if _is_root_folder_id(explicit_id) else explicit_id
        if not self.create_folder_if_missing:
            raise RuntimeError(f"目标目录不存在或未允许自动创建：{target_path}")
        return self._cmcc().ensure_path(target_path)

    def _native_status(self, task_id: str) -> str:
        # 官方接口的任务完成只代表云端保存任务受理/完成，不能代表 OpenList 已可见、
        # Organizer 已整理、标准目录已有视频文件。最终完成由入库闭环统一确认。
        if self.mark_done_after_submit:
            return JOB_SUBMITTED
        if not task_id:
            return JOB_SUBMITTED
        for _ in range(3):
            try:
                task = self._cmcc().get_task(task_id)
            except Exception:  # noqa: BLE001
                return JOB_SUBMITTED
            text = str(task.get("status") or task.get("taskStatus") or "").strip().lower()
            if text in {"success", "done", "2", "completed"}:
                return JOB_SUBMITTED
            if text in {"failed", "fail", "3", "error"}:
                return JOB_FAILED
            time.sleep(1)
        return JOB_SUBMITTED

    def _with_target_root(self, value: Any) -> str:
        path = _normalize_cloud_path(value)
        root = self.target_root_path
        if not root:
            return path
        if not path:
            return root
        if path.casefold() == root.casefold() or path.casefold().startswith(f"{root.casefold()}/"):
            return path
        return _join_cloud_path(root, path)


def _normalize_share_item(item: dict[str, Any]) -> dict[str, Any]:
    is_dir = _share_item_is_dir(item)
    fid = str(item.get("fid") or item.get("id") or "").strip()
    name = str(item.get("name") or item.get("fileName") or fid or "未命名文件").strip()
    path = str(item.get("path") or "").strip()
    token = str(item.get("share_fid_token") or "").strip()
    if not path and token:
        path = _path_from_token(token)
    return {
        "fid": fid,
        "id": fid,
        "name": name,
        "fileName": name,
        "type": "dir" if is_dir else "file",
        "isFolder": is_dir,
        "is_dir": is_dir,
        "dir": is_dir,
        "size": item.get("size") or item.get("fileSize") or item.get("contentSize"),
        "path": path or fid,
        "share_fid_token": token,
        "can_expand": bool(is_dir and fid),
        "raw": item.get("raw") if isinstance(item.get("raw"), dict) else item,
    }


def _normalize_selected_files(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows[:300]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("fileName") or "").strip()
        fid = str(item.get("fid") or item.get("id") or "").strip()
        token = str(item.get("share_fid_token") or "").strip()
        path = str(item.get("path") or "").strip() or _path_from_token(token) or fid
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({"name": name or fid or path, "fid": fid, "path": path, "share_fid_token": token, "type": "file", "is_dir": False})
    return result


def _normalize_selected_folders(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows[:100]:
        if isinstance(item, str):
            fid, name, path, token = item.strip(), item.strip(), item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("fileName") or "").strip()
            fid = str(item.get("fid") or item.get("id") or "").strip()
            token = str(item.get("share_fid_token") or "").strip()
            path = str(item.get("path") or "").strip() or _path_from_token(token) or fid
        else:
            continue
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({"name": name or fid or path, "fid": fid, "path": path, "share_fid_token": token, "type": "dir", "is_dir": True})
    return result


def _path_from_token(token: str) -> str:
    if not token:
        return ""
    try:
        data = json.loads(token)
    except Exception:
        return ""
    return str(data.get("path") or "").strip() if isinstance(data, dict) else ""


def _selected_item_save_value(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("fid") or item.get("id") or item.get("file_id") or item.get("path") or _path_from_token(str(item.get("share_fid_token") or "")) or "").strip()


def _normalize_share_fid(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", "-1", "root-files", "0"}:
        return ""
    return text


def _normalize_cloud_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if ":" in text.split("/", 1)[0]:
        text = text.split(":", 1)[1]
    return "/".join(segment.strip() for segment in text.strip("/").split("/") if segment.strip())


def _join_cloud_path(*parts: Any) -> str:
    segments: list[str] = []
    for part in parts:
        normalized = _normalize_cloud_path(part)
        if normalized:
            segments.extend(segment for segment in normalized.split("/") if segment)
    return "/".join(segments)


def _same_cloud_path(left: Any, right: Any) -> bool:
    return _normalize_cloud_path(left).lower() == _normalize_cloud_path(right).lower()


def _is_root_folder_id(value: Any) -> bool:
    text = str(value or "").strip()
    return text in {"", "/", "-11", "root", "0"}


def _share_item_is_dir(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("dir") is True or item.get("isFolder") is True or item.get("is_dir") is True:
        return True
    if item.get("dir") is False or item.get("isFolder") is False or item.get("is_dir") is False:
        return False
    item_type = str(item.get("type") or item.get("fileType") or "").strip().lower()
    return item_type in {"dir", "folder", "catalog"}


def _dict_of_str(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item).strip() for key, item in value.items() if str(item).strip()}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
