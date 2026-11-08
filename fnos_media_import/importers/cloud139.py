from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from ..cmcc.auth import CmccAuthConfigError, CmccAuthProvider
from ..cmcc.client import CmccApiError, CmccClient
from ..constants import JOB_FAILED, JOB_SUBMITTED
from ..media_path_rules import append_resource_dir, sanitize_resource_dir_name
from ..media_file_types import VIDEO_EXTENSIONS
from ..provider_errors import provider_error_details
from .base import AdapterCheckResult, ImportResult


class Cloud139Importer:
    """139 移动云官方直转适配器。

    分享预览、目录展开、单文件/文件夹转存都复用项目现有的
    cmcc_upload 官方 API 认证，不需要额外转存服务。
    """

    def __init__(
        self,
        config: dict[str, Any],
        cmcc_config: dict[str, Any] | None = None,
        *,
        authorization_update_callback: Callable[[dict[str, Any]], bool] | None = None,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.cmcc_config = cmcc_config if isinstance(cmcc_config, dict) else {}
        self.authorization_update_callback = (
            authorization_update_callback if callable(authorization_update_callback) else None
        )
        self.timeout = int(self.config.get("timeout", self.cmcc_config.get("timeout", 30)) or 30)
        # Destination roots/mount identities are frozen in the V1 storage
        # target binding.  They are deliberately not read from importer
        # configuration (which may only contain provider account settings).
        self.target_root_path = ""
        self.fnos_mount_name = ""
        self.check_before_save = _as_bool(self.config.get("check_before_save"), True)
        self.refresh_after_submit = _as_bool(self.config.get("refresh_after_submit"), True)
        self.refresh_delay_seconds = max(0, int(self.config.get("refresh_delay_seconds", 90) or 0))
        self.mark_done_after_submit = _as_bool(self.config.get("mark_done_after_submit"), True)
        self.create_folder_if_missing = _as_bool(self.config.get("create_folder_if_missing"), True)
        self._cmcc_client: CmccClient | None = None

    @property
    def configured(self) -> bool:
        if self.cmcc_config.get("enabled") is False:
            return False
        if str(self.cmcc_config.get("access_token") or self.cmcc_config.get("authorization") or "").strip():
            return True
        return False

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
            # Building the client only validates local configuration.  Use a
            # minimal read-only request so an expired or malformed Basic token
            # cannot be accepted as a healthy Provider account.
            client.list_dir("/", page_size=1, max_pages=1)
            # This API scopes its authenticated request to account=<phone>.
            # A successful scoped read proves this account identifier, unlike
            # the editable account name or the generic configuration summary.
            phone = str(client.phone or "").strip()
            if phone.isdigit() and len(phone) == 11:
                details["identity_id"] = f"mobile:account:{phone}"
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
            details = _cloud139_error_details(exc)
            return False, {
                "success": False, "message": f"139 官方分享检测失败：{exc}", "provider": "cmcc_native",
                "error_code": details["error_code"], "error_details": details,
            }
        share_title = title or "139 分享"
        normalized = [_normalize_share_item(item) for item in items]
        try:
            scan = self._scan_share_videos(link_id, passcode, pdir)
        except (CmccApiError, CmccAuthConfigError, RuntimeError) as exc:
            scan = {
                "complete": False,
                "video_file_count": 0,
                "total_size": 0,
                "folder_count": 0,
                "error": str(exc),
            }
        return True, {
            "success": True,
            "message": "139 官方分享检测通过",
            "provider": "cmcc_native",
            "data": {
                "share": {
                    "title": share_title,
                    "name": share_title,
                    "status": "valid",
                    # A root directory is not a playable video.  The public
                    # preflight uses this *recursive* count to reject empty
                    # shell shares before they create a job.
                    "file_count": scan["video_file_count"] if scan["complete"] else "",
                    "video_file_count": scan["video_file_count"],
                    "root_file_count": len(normalized),
                    "total_size": scan["total_size"],
                    "folder_count": scan["folder_count"],
                    "scan_complete": bool(scan["complete"]),
                    **({"scan_error": scan["error"]} if scan.get("error") else {}),
                    "link_id": link_id,
                    "pdir_fid": pdir,
                },
                "items": normalized,
            },
        }

    def _scan_share_videos(
        self,
        link_id: str,
        password: str,
        root: str,
        *,
        max_depth: int = 6,
        max_entries: int = 10000,
    ) -> dict[str, Any]:
        """Recursively count video files without trusting a folder-only root."""

        stack: list[tuple[str, int]] = [(root or "root", 0)]
        seen: set[str] = set()
        video_count = 0
        total_size = 0
        folder_count = 0
        complete = True
        while stack:
            folder, depth = stack.pop()
            if folder in seen:
                continue
            seen.add(folder)
            if depth > max_depth:
                complete = False
                continue
            rows = self._cmcc().list_share_dir(link_id, password=password, pdir_fid=folder)
            if len(rows) > max_entries:
                complete = False
                rows = rows[:max_entries]
            for raw in rows:
                item = _normalize_share_item(raw)
                if item["is_dir"]:
                    folder_count += 1
                    fid = str(item.get("fid") or "").strip()
                    if fid and depth < max_depth:
                        stack.append((fid, depth + 1))
                    elif depth >= max_depth:
                        complete = False
                    continue
                if not _is_video_name(str(item.get("name") or "")):
                    continue
                video_count += 1
                total_size += _size_bytes(item.get("size"))
        return {
            "complete": complete,
            "video_file_count": video_count,
            "total_size": total_size,
            "folder_count": folder_count,
        }

    def list_files(self, share_url: str, fid: str, password: str = "", title: str = "") -> dict[str, Any]:
        if not self.configured:
            return {"success": False, "message": self.check_configuration().message, "items": [], "provider": "cmcc_native"}
        try:
            link_id, parsed_password, pdir_fid = CmccClient.extract_share_url(share_url)
            pdir = _normalize_share_fid(fid) or pdir_fid or "root"
            items = [_normalize_share_item(item) for item in self._cmcc().list_share_dir(link_id, password=password or parsed_password, pdir_fid=pdir)]
        except (CmccApiError, CmccAuthConfigError, RuntimeError) as exc:
            details = _cloud139_error_details(exc)
            return {
                "success": False, "message": f"139 官方目录读取失败：{exc}", "items": [], "provider": "cmcc_native",
                "error_code": details["error_code"], "error_details": details,
            }
        return {
            "success": True,
            "message": "139 官方目录读取成功",
            "provider": "cmcc_native",
            "items": items,
            "data": {"items": items, "share": {"title": title or "139 分享", "pdir_fid": pdir}},
        }

    def list_folders(self, parent_folder_id: str = "/") -> list[dict[str, Any]]:
        """List folders in the user's own 139 drive for the storage picker."""
        if not self.configured:
            raise RuntimeError(self.check_configuration().message)
        parent = str(parent_folder_id or "/").strip() or "/"
        folders: list[dict[str, Any]] = []
        for row in self._cmcc().list_dir(parent):
            folder_id, name, is_folder = _cmcc_drive_item(row)
            if is_folder and folder_id:
                folders.append(
                    {
                        "folder_id": folder_id,
                        "name": name,
                        "parent_folder_id": parent,
                    }
                )
        return folders

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
                raw_data={
                    "selection": {"files": files, "folders": folders},
                    "default_full_share": default_full_share, "error": str(exc),
                    **_cloud139_error_details(exc),
                },
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
        del category_key
        category = category if isinstance(category, dict) else {}
        # V1 callers pass the immutable native locator value as ``save_path``.
        # Do not derive a destination from removed category/global path keys;
        # an empty value is an explicit configuration error.
        explicit = _normalize_cloud_path(save_path)
        if not explicit:
            locator = category.get("native_locator")
            if isinstance(locator, dict):
                explicit = _normalize_cloud_path(locator.get("value"))
        return self._with_target_root(explicit) if explicit else ""

    def _cmcc(self) -> CmccClient:
        if self._cmcc_client is None:
            self._cmcc_client = CmccClient(
                CmccAuthProvider(
                    self.cmcc_config,
                    authorization_update_callback=self.authorization_update_callback,
                ),
                timeout=self.timeout,
            )
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
        del category_key
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


def _cloud139_error_details(exc: BaseException) -> dict[str, Any]:
    details = provider_error_details(exc)
    code = details["error_code"]
    if code and code not in {"SHARE_INVALID", "NETWORK_TRANSIENT", "AUTH_EXPIRED", "AUTH_CONFIG"}:
        if not (code.startswith("HTTP_") and code[5:].isdigit()):
            # CMCC business codes were previously diagnostics only.  Preserve
            # the legacy retry fallback unless this change classified a known
            # resource, transport or HTTP rejection.
            details.setdefault("provider_code", code)
            details["error_code"] = ""
    return details


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
        dedupe_key = fid or token or path
        if not path or not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
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
        dedupe_key = fid or token or path
        if not path or not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
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


def _cmcc_drive_item(item: Any) -> tuple[str, str, bool]:
    """Extract (folder_id, name, is_folder) from a 139 drive list row."""
    if not isinstance(item, dict):
        return "", "", False
    folder_id = str(
        item.get("fileId")
        or item.get("file_id")
        or item.get("id")
        or item.get("contentID")
        or item.get("contentId")
        or item.get("catalogID")
        or item.get("caID")
        or ""
    ).strip()
    name = str(
        item.get("name")
        or item.get("fileName")
        or item.get("contentName")
        or item.get("catalogName")
        or item.get("caName")
        or folder_id
        or "未命名目录"
    ).strip()
    is_folder = _share_item_is_dir(item)
    if not is_folder:
        raw_type = str(
            item.get("type") or item.get("fileType") or item.get("category") or ""
        ).strip().lower()
        is_folder = raw_type in {"folder", "dir", "directory", "catalog", "1"}
    return folder_id, name, is_folder


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


_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS


def _is_video_name(name: str) -> bool:
    return os.path.splitext(str(name or "").lower())[1] in _VIDEO_EXTENSIONS


def _size_bytes(value: Any) -> int:
    try:
        number = int(float(str(value or "0").replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
