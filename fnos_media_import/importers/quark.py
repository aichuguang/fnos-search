from __future__ import annotations

import time
import re
from typing import Any
from urllib.parse import urlparse

from ..constants import JOB_FAILED, JOB_WAITING_TRANSFER
from ..http_client import HttpClient
from ..media_path_rules import append_resource_dir, sanitize_resource_dir_name, should_force_resource_dir
from .base import ImportResult


class QuarkImporter:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config.get("auto_save_url", "")).rstrip("/")
        self.token = str(config.get("token", ""))
        self.timeout = int(config.get("timeout", 20))
        self.check_before_save = bool(config.get("check_before_save", True))
        self.run_immediately = bool(config.get("run_immediately", True))
        self.http = HttpClient(timeout=self.timeout, verify_tls=False, trust_env=False)

    def import_resource(
        self,
        title: str,
        share_url: str,
        save_path: str,
        pattern: str = r"(?:【.*?】)?(.*)",
        replace: str = r"\1",
        category_key: str = "",
        category: dict[str, Any] | None = None,
        quark_selection: dict[str, Any] | None = None,
    ) -> ImportResult:
        del category
        if not self.base_url:
            return ImportResult(False, JOB_FAILED, "Quark 自动转存地址未配置", target_path=save_path)
        if not self.token:
            return ImportResult(False, JOB_FAILED, "Quark 自动转存 Token 未配置", target_path=save_path)

        check_data: dict[str, Any] = {}
        selection = self._normalize_selection(quark_selection)
        selection_enabled = selection.get("mode") in {"root_dirs", "root_items", "subdir_items"}
        if self.check_before_save or selection_enabled:
            ok, check_data = self.check_share(share_url, title)
            if not ok:
                return ImportResult(False, JOB_FAILED, "夸克分享链接检测失败或资源已失效", target_path=save_path, raw_data=check_data)

        file_count = self._extract_file_count(check_data)
        effective_save_path = append_resource_dir(save_path, title) if should_force_resource_dir(category_key, file_count) else save_path

        try:
            task_payloads, selection_plan = self._build_task_payloads(
                title=title,
                share_url=share_url,
                save_path=effective_save_path,
                pattern=pattern,
                replace=replace,
                check_data=check_data,
                selection=selection,
            )
        except ValueError as exc:
            return ImportResult(
                False,
                JOB_FAILED,
                str(exc),
                target_path=effective_save_path,
                raw_data={
                    "check": check_data,
                    "selection": selection,
                    "path_rule": self._path_rule_payload(category_key, save_path, effective_save_path, file_count),
                },
            )

        save_results: list[dict[str, Any]] = []
        task_ids: list[str] = []
        for payload in task_payloads:
            status_code, result = self.http.post_json(self._token_url("/api/add_task"), payload, timeout=self.timeout)
            success = bool(isinstance(result, dict) and result.get("success"))
            save_results.append({"status_code": status_code, "result": result, "task": self._safe_task_payload(payload)})
            if status_code >= 400 or not success:
                return ImportResult(
                    False,
                    JOB_FAILED,
                    self._extract_message(result, f"提交 Quark 转存失败：HTTP {status_code}"),
                    target_path=effective_save_path,
                    raw_data={
                        "check": check_data,
                        "save": save_results,
                        "selection": selection_plan,
                        "path_rule": self._path_rule_payload(category_key, save_path, effective_save_path, file_count),
                    },
                )
            if isinstance(result, dict):
                task_id = str(result.get("id") or result.get("task_id") or result.get("data", {}).get("id") or "")
                if task_id:
                    task_ids.append(task_id)

        if self.run_immediately:
            time.sleep(1)
            run_payload = {"tasklist": [{**payload, "runweek": [1, 2, 3, 4, 5, 6, 7]} for payload in task_payloads]}
            _, run_result = self.http.post_json(self._token_url("/run_script_now"), run_payload, timeout=self.timeout)
        else:
            run_result = {}

        return ImportResult(
            True,
            JOB_WAITING_TRANSFER,
            "已提交 Quark 自动转存，等待 rclone 搬运到移动云" if len(task_payloads) == 1 else f"已提交 {len(task_payloads)} 个 Quark 勾选转存任务，等待 rclone 搬运到移动云",
            external_task_id=",".join(task_ids),
            target_path=effective_save_path,
            raw_data={
                "check": check_data,
                "save": save_results[0]["result"] if len(save_results) == 1 else save_results,
                "run_now": run_result,
                "selection": selection_plan,
                "path_rule": self._path_rule_payload(category_key, save_path, effective_save_path, file_count),
            },
        )

    def check_share(self, share_url: str, title: str = "") -> tuple[bool, dict[str, Any]]:
        if not self.base_url:
            return False, {"success": False, "message": "Quark 自动转存地址未配置，无法检测资源详情"}
        if not self.token:
            return False, {"success": False, "message": "Quark 自动转存 Token 未配置，无法检测资源详情"}
        payload = {
            "shareurl": share_url,
            "stoken": "",
            "task": {
                "taskname": title or "temp_check",
                "shareurl": share_url,
                "savepath": "/temp_check",
                "addition": {"aria2": {"auto_download": False}},
            },
            "magic_regex": {},
        }
        try:
            status_code, data = self.http.post_json(self._token_url("/get_share_detail"), payload, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return False, {"success": False, "message": f"Quark 资源详情检测失败：{exc}"}
        if status_code >= 400 or not isinstance(data, dict):
            return False, data if isinstance(data, dict) else {"data": data}
        share = data.get("data", {}).get("share", {})
        success = bool(data.get("success") and share.get("status") == 1)
        return success, data

    def list_files(self, pwd_id: str, fid: str, stoken: str) -> dict[str, Any]:
        if not self.base_url:
            return {"success": False, "message": "Quark 自动转存地址未配置"}
        if not self.token:
            return {"success": False, "message": "Quark 自动转存 Token 未配置"}
        share_url = f"https://pan.quark.cn/s/{pwd_id}#/list/share/{fid}"
        payload = {
            "shareurl": share_url,
            "stoken": stoken,
            "task": {
                "taskname": "temp_fetch_list",
                "shareurl": share_url,
                "savepath": "/temp",
                "addition": {},
            },
            "magic_regex": {},
        }
        try:
            _, data = self.http.post_json(self._token_url("/get_share_detail"), payload, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"Quark 文件列表获取失败：{exc}"}
        return data

    def _token_url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.base_url}{path}{sep}token={self.token}"

    @staticmethod
    def _extract_message(data: Any, default: str) -> str:
        if isinstance(data, dict):
            return str(data.get("message") or data.get("msg") or data.get("error") or default)
        return default

    @classmethod
    def _extract_file_count(cls, payload: Any) -> int:
        value = cls._find_first_value(payload, {"file_num", "file_count", "fileCount"})
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            rows = cls._extract_file_rows(payload)
            return len(rows)

    @classmethod
    def _find_first_value(cls, payload: Any, keys: set[str]) -> Any:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys and value not in (None, ""):
                    return value
            for value in payload.values():
                found = cls._find_first_value(value, keys)
                if found not in (None, ""):
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = cls._find_first_value(item, keys)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _extract_file_rows(cls, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        for key in (
            "list",
            "files",
            "file_list",
            "fileList",
            "items",
            "children",
            "contents",
            "folders",
            "folderList",
            "folder_list",
            "shareFolders",
            "share_folders",
            "catalogList",
            "contentList",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                rows = [item for item in value if isinstance(item, dict)]
                if rows:
                    return rows
        for value in payload.values():
            if isinstance(value, dict):
                rows = cls._extract_file_rows(value)
                if rows:
                    return rows
        return []

    @staticmethod
    def _path_rule_payload(category_key: str, original_path: str, effective_path: str, file_count: int) -> dict[str, Any]:
        return {
            "category": category_key,
            "original_save_path": original_path,
            "effective_save_path": effective_path,
            "resource_dir_applied": original_path != effective_path,
            "file_count": file_count,
        }

    def _build_task_payloads(
        self,
        *,
        title: str,
        share_url: str,
        save_path: str,
        pattern: str,
        replace: str,
        check_data: dict[str, Any],
        selection: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        mode = str(selection.get("mode") or "all")
        if mode not in {"root_dirs", "root_items", "subdir_items"}:
            payload = self._task_payload(title=title, share_url=share_url, save_path=save_path, pattern=pattern, replace=replace)
            return [payload], {"mode": "all", "task_count": 1, "tasks": [self._safe_task_payload(payload)]}

        pwd_id = self._extract_pwd_id(share_url) or str(self._find_first_value(check_data, {"pwd_id", "pwdId", "share_id", "shareId"}) or "")
        if not pwd_id:
            raise ValueError("无法识别 Quark 分享 ID，暂不能按勾选范围转存")

        if mode == "root_dirs":
            root_rows = self._extract_file_rows(check_data)
            row_map = self._row_map_by_fid(root_rows)
            selected_dirs: list[dict[str, Any]] = []
            for requested in selection.get("selected_dirs") or []:
                fid = str(requested.get("fid") or "").strip() if isinstance(requested, dict) else ""
                row = row_map.get(fid)
                item = self._file_item(row) if row else {}
                if item.get("fid") and item.get("is_dir"):
                    selected_dirs.append(item)
            selected_dirs = self._dedupe_items(selected_dirs)
            if not selected_dirs:
                raise ValueError("未找到可转存的夸克根目录文件夹，请重新展开资源后勾选")
            payloads = [
                self._task_payload(
                    title=self._task_title(title, item["name"]),
                    share_url=self._subdir_share_url(share_url, pwd_id, item["fid"]),
                    save_path=self._join_save_path(save_path, item["name"]),
                    pattern=pattern,
                    replace=replace,
                )
                for item in selected_dirs
            ]
            return payloads, {
                "mode": "root_dirs",
                "selected_count": len(selected_dirs),
                "selected_dirs": selected_dirs,
                "task_count": len(payloads),
                "tasks": [self._safe_task_payload(payload) for payload in payloads],
            }

        if mode == "root_items":
            root_rows = self._extract_file_rows(check_data)
            row_map = self._row_map_by_fid(root_rows)
            selected_items: list[dict[str, Any]] = []
            for requested in selection.get("selected_items") or []:
                fid = str(requested.get("fid") or "").strip() if isinstance(requested, dict) else ""
                row = row_map.get(fid)
                item = self._file_item(row) if row else {}
                if item.get("fid") and not item.get("is_dir"):
                    selected_items.append(item)
            selected_items = self._dedupe_items(selected_items)
            if not selected_items:
                raise ValueError("未找到可转存的夸克根层文件，请重新展开资源后勾选")
            payload = self._task_payload(
                title=title,
                share_url=share_url,
                save_path=save_path,
                pattern=self._exact_name_pattern([item["name"] for item in selected_items]),
                replace=r"\1",
            )
            return [payload], {
                "mode": "root_items",
                "selected_count": len(selected_items),
                "selected_items": selected_items,
                "task_count": 1,
                "tasks": [self._safe_task_payload(payload)],
            }

        base_dir = selection.get("base_dir") if isinstance(selection.get("base_dir"), dict) else {}
        base_fid = str(base_dir.get("fid") or "").strip()
        if not base_fid:
            raise ValueError("未指定要进入选择的夸克文件夹，请重新勾选")

        root_rows = self._extract_file_rows(check_data)
        base_row = self._row_map_by_fid(root_rows).get(base_fid)
        base_item = self._file_item(base_row) if base_row else {}
        if not base_item.get("fid") or not base_item.get("is_dir"):
            raise ValueError("指定的夸克文件夹不存在或不是文件夹，请重新展开资源后选择")

        stoken = str(self._find_first_value(check_data, {"stoken", "share_stoken", "shareToken", "share_token"}) or "")
        children_data = self.list_files(pwd_id=pwd_id, fid=base_item["fid"], stoken=stoken)
        if not isinstance(children_data, dict) or children_data.get("success", True) is False:
            raise ValueError(self._extract_message(children_data, "指定文件夹内容读取失败，请稍后重试"))
        child_rows = self._extract_file_rows(children_data)
        child_map = self._row_map_by_fid(child_rows)

        selected_items: list[dict[str, Any]] = []
        for requested in selection.get("selected_items") or []:
            fid = str(requested.get("fid") or "").strip() if isinstance(requested, dict) else ""
            row = child_map.get(fid)
            item = self._file_item(row) if row else {}
            if item.get("fid"):
                selected_items.append(item)
        selected_items = self._dedupe_items(selected_items)
        if not selected_items:
            raise ValueError("请至少选择一个要保存的文件或文件夹")

        selected_dirs = [item for item in selected_items if item.get("is_dir")]
        selected_files = [item for item in selected_items if not item.get("is_dir")]
        payloads: list[dict[str, Any]] = []
        base_save_path = self._join_save_path(save_path, base_item["name"])
        if selected_files:
            payloads.append(
                self._task_payload(
                    title=self._task_title(title, base_item["name"]),
                    share_url=self._subdir_share_url(share_url, pwd_id, base_item["fid"]),
                    save_path=base_save_path,
                    pattern=self._exact_name_pattern([item["name"] for item in selected_files]),
                    replace=r"\1",
                )
            )
        for item in selected_dirs:
            payloads.append(
                self._task_payload(
                    title=self._task_title(title, f"{base_item['name']} - {item['name']}"),
                    share_url=self._subdir_share_url(share_url, pwd_id, item["fid"]),
                    save_path=self._join_save_path(base_save_path, item["name"]),
                    pattern=pattern,
                    replace=replace,
                )
            )

        return payloads, {
            "mode": "subdir_items",
            "base_dir": base_item,
            "selected_count": len(selected_items),
            "selected_files_count": len(selected_files),
            "selected_dirs_count": len(selected_dirs),
            "selected_items": selected_items,
            "task_count": len(payloads),
            "tasks": [self._safe_task_payload(payload) for payload in payloads],
        }

    @staticmethod
    def _normalize_selection(selection: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(selection, dict):
            return {"mode": "all"}
        mode = str(selection.get("mode") or "all").strip()
        if mode not in {"all", "root_dirs", "root_items", "subdir_items"}:
            mode = "all"
        return {**selection, "mode": mode}

    @staticmethod
    def _task_payload(title: str, share_url: str, save_path: str, pattern: str, replace: str) -> dict[str, Any]:
        return {
            "taskname": title,
            "shareurl": share_url,
            "savepath": save_path,
            "pattern": pattern,
            "replace": replace,
            "runweek": [],
        }

    @staticmethod
    def _task_title(title: str, suffix: str) -> str:
        base = str(title or "未命名资源").strip() or "未命名资源"
        child = sanitize_resource_dir_name(suffix, fallback="", max_length=80)
        if not child:
            return base
        candidate = f"{base} - {child}"
        return candidate[:180].rstrip(" .-_") or base

    @staticmethod
    def _join_save_path(base_path: str, child: str) -> str:
        return append_resource_dir(base_path, sanitize_resource_dir_name(child, fallback="未命名文件夹"))

    @staticmethod
    def _extract_pwd_id(url: str) -> str:
        parsed = urlparse(str(url or "") if "://" in str(url or "") else f"https://{url}")
        path = parsed.path.strip("/")
        if "/s/" in f"/{path}":
            return f"/{path}".split("/s/", 1)[1].split("/", 1)[0].split("#", 1)[0]
        return ""

    @staticmethod
    def _subdir_share_url(original_url: str, pwd_id: str, fid: str) -> str:
        parsed = urlparse(str(original_url or "") if "://" in str(original_url or "") else f"https://{original_url}")
        scheme = parsed.scheme or "https"
        host = parsed.netloc or "pan.quark.cn"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{host}/s/{pwd_id}{query}#/list/share/{fid}"

    @staticmethod
    def _exact_name_pattern(names: list[str]) -> str:
        escaped = [re.escape(str(name or "")) for name in names if str(name or "")]
        if not escaped:
            return r"a^"
        return rf"^({'|'.join(escaped)})$"

    @classmethod
    def _row_map_by_fid(cls, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = cls._file_item(row)
            fid = str(item.get("fid") or "")
            if fid and fid not in result:
                result[fid] = row
        return result

    @classmethod
    def _dedupe_items(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            fid = str(item.get("fid") or "")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            result.append(item)
        return result

    @staticmethod
    def _file_item(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            item = {}
        name = (
            item.get("file_name")
            or item.get("fileName")
            or item.get("name")
            or item.get("title")
            or item.get("folderName")
            or item.get("catalogName")
            or item.get("caName")
            or item.get("fid")
            or item.get("id")
            or "未命名文件"
        )
        type_text = str(item.get("file_type") or item.get("fileType") or item.get("type") or item.get("kind") or "").lower()
        is_dir = bool(
            item.get("dir")
            or item.get("is_dir")
            or item.get("isDir")
            or item.get("isFolder")
            or item.get("folder")
            or item.get("folder_id")
            or item.get("folderName")
            or type_text in {"folder", "dir", "directory", "catalog"}
        )
        fid = str(
            item.get("fid")
            or item.get("folder_id")
            or item.get("id")
            or item.get("fileId")
            or item.get("file_id")
            or item.get("contentId")
            or item.get("contentID")
            or item.get("catalogId")
            or item.get("catalogID")
            or item.get("caID")
            or item.get("caId")
            or item.get("shareFolderId")
            or item.get("share_folder_id")
            or ""
        )
        return {"fid": fid, "name": str(name), "is_dir": is_dir}

    @staticmethod
    def _safe_task_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "taskname": payload.get("taskname"),
            "shareurl": payload.get("shareurl"),
            "savepath": payload.get("savepath"),
            "pattern": payload.get("pattern"),
            "replace": payload.get("replace"),
        }
