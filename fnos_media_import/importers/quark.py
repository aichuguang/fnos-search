from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..constants import JOB_FAILED, JOB_SUBMITTED
from ..http_client import HttpClient
from ..media_path_rules import append_resource_dir, should_force_resource_dir
from .base import AdapterCheckResult, ImportResult

class QuarkApiError(RuntimeError):
    """Raised when the Quark web API rejects a request."""


class QuarkImporter:
    """Built-in Quark web API client.

    The implementation is intentionally independent from quark-auto-save. That
    project was used only to verify the remote API contract.
    """

    BASE_URL = "https://drive-pc.quark.cn"
    ACCOUNT_URL = "https://pan.quark.cn"

    def __init__(
        self,
        config: dict[str, Any],
        cookie_update_callback: Callable[[dict[str, str]], Any] | None = None,
        configuration_error: str = "",
    ) -> None:
        values = dict(config or {})
        self.cookie = str(values.get("cookie") or values.get("cookies") or "").strip()
        self.configuration_error = str(configuration_error or "").strip()
        self.cookie_update_callback = (
            cookie_update_callback if callable(cookie_update_callback) else None
        )
        self.cookie_update_error = ""
        self._persisted_cookie = self.cookie
        self.base_url = str(values.get("base_url") or self.BASE_URL).rstrip("/")
        self.timeout = max(5, min(int(values.get("timeout") or 20), 120))
        self.http = HttpClient(timeout=self.timeout, verify_tls=True, trust_env=False)
        self.http.session.headers.update(
            {
                "Cookie": self.cookie,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Referer": "https://pan.quark.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "quark-cloud-drive/3.0 Chrome/120 Safari/537.36"
                ),
            }
        )
        self.http.session.hooks.setdefault("response", []).append(
            self._capture_runtime_cookie
        )

    def _capture_runtime_cookie(self, response: Any, *_args: Any, **_kwargs: Any) -> Any:
        """Keep Quark's rolling ``__puus`` cookie current and durable."""
        puus = self._response_cookie_value(response, "__puus")
        if puus:
            merged = self._merge_cookie(self.cookie, "__puus", puus)
            if merged != self.cookie:
                self.cookie = merged
                self.http.session.headers["Cookie"] = merged
        if (
            self.cookie_update_callback is not None
            and self.cookie != self._persisted_cookie
        ):
            try:
                self.cookie_update_callback({"cookie": self.cookie})
                self._persisted_cookie = self.cookie
                self.cookie_update_error = ""
            except Exception as exc:  # noqa: BLE001
                # A successful Provider request must not fail because local
                # persistence is temporarily unavailable. The in-memory cookie
                # remains current for subsequent requests in this execution.
                self.cookie_update_error = str(exc)
        return response

    @staticmethod
    def _response_cookie_value(response: Any, name: str) -> str:
        cookies = getattr(response, "cookies", None)
        if cookies is not None:
            try:
                for cookie in cookies:
                    if str(getattr(cookie, "name", "")) == name:
                        return str(getattr(cookie, "value", "") or "").strip()
            except TypeError:
                pass
        headers = getattr(response, "headers", {}) or {}
        raw = str(headers.get("Set-Cookie") or headers.get("set-cookie") or "")
        match = re.search(rf"(?:^|[;,]\s*){re.escape(name)}=([^;]+)", raw)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _merge_cookie(cookie_header: str, name: str, value: str) -> str:
        pairs: list[tuple[str, str]] = []
        replaced = False
        for item in str(cookie_header or "").split(";"):
            key, separator, current = item.strip().partition("=")
            if not separator or not key:
                continue
            if key == name:
                if not replaced:
                    pairs.append((name, value))
                    replaced = True
                continue
            pairs.append((key, current))
        if not replaced:
            pairs.append((name, value))
        return "; ".join(f"{key}={current}" for key, current in pairs)

    def check_configuration(self) -> AdapterCheckResult:
        if not self.cookie:
            return AdapterCheckResult(
                False,
                "account_unavailable" if self.configuration_error else "unconfigured",
                self._configuration_message(),
            )
        return AdapterCheckResult(True, "ready", "夸克账号凭证已配置")

    def _configuration_message(self) -> str:
        return self.configuration_error or "夸克 Cookie 未配置"

    def probe(self) -> AdapterCheckResult:
        configured = self.check_configuration()
        if not configured.ok:
            return configured
        try:
            payload = self._get(
                "/account/info",
                base=self.ACCOUNT_URL,
                params={"fr": "pc", "platform": "pc"},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            identity = str(
                data.get("nickname")
                or data.get("user_name")
                or data.get("email")
                or data.get("phone")
                or ""
            )
            suffix = f"：{identity}" if identity else ""
            return AdapterCheckResult(
                True,
                "ready",
                f"夸克账号检测通过{suffix}",
                {"identity_hint": identity} if identity else {},
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterCheckResult(False, "probe_failed", f"夸克账号检测失败：{exc}")

    def check_share(
        self,
        share_url: str,
        title: str = "",
        password: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        del title
        if not self.cookie:
            return False, {"success": False, "message": self._configuration_message()}
        try:
            pwd_id = self._extract_pwd_id(share_url)
            if not pwd_id:
                raise QuarkApiError("无法识别夸克分享 ID")
            token_payload = self._post(
                "/1/clouddrive/share/sharepage/token",
                {"pwd_id": pwd_id, "passcode": str(password or "")},
                params={"pr": "ucpro", "fr": "pc"},
            )
            token_data = (
                token_payload.get("data")
                if isinstance(token_payload.get("data"), dict)
                else token_payload
            )
            stoken = str(
                self._find_first_value(
                    token_data,
                    {"stoken", "share_token", "shareToken"},
                )
                or ""
            )
            if not stoken:
                raise QuarkApiError("分享口令无效或接口未返回 stoken")
            detail = self._get_share_detail(pwd_id, "0", stoken, fetch_share=1)
            rows = self._extract_file_rows(detail)
            return True, {
                "success": True,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "items": rows,
                "data": detail.get("data") if isinstance(detail.get("data"), dict) else detail,
                "raw": detail,
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"success": False, "message": str(exc)}

    def list_files(self, pwd_id: str, fid: str, stoken: str) -> dict[str, Any]:
        if not self.cookie:
            return {"success": False, "message": self._configuration_message()}
        try:
            return self._get_share_detail(pwd_id, fid or "0", stoken)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"夸克文件列表获取失败：{exc}"}

    def list_folders(self, parent_folder_id: str = "0") -> list[dict[str, Any]]:
        """List folders in the user's own Quark drive for the storage picker."""
        if not self.cookie:
            raise QuarkApiError(self._configuration_message())
        parent = str(parent_folder_id or "0")
        payload = self._get(
            "/1/clouddrive/file/sort",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "pdir_fid": parent,
                "_page": 1,
                "_size": 200,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
            },
        )
        folders: list[dict[str, Any]] = []
        for row in self._extract_file_rows(payload):
            item = self._file_item(row)
            if item["is_dir"] and item["fid"]:
                folders.append(
                    {
                        "folder_id": item["fid"],
                        "name": item["name"],
                        "parent_folder_id": parent,
                    }
                )
        return folders

    def import_resource(
        self,
        title: str,
        share_url: str,
        save_path: str,
        pattern: str = "",
        replace: str = "",
        category_key: str = "",
        category: dict[str, Any] | None = None,
        quark_selection: dict[str, Any] | None = None,
        password: str = "",
        **_kwargs: Any,
    ) -> ImportResult:
        del category, pattern, replace
        if not self.cookie:
            return ImportResult(False, JOB_FAILED, self._configuration_message(), target_path=save_path)
        selection = self._normalize_selection(quark_selection)
        try:
            ok, checked = self.check_share(share_url, title, password=password)
            if not ok:
                raise QuarkApiError(str(checked.get("message") or "夸克分享检查失败"))
            file_count = self._extract_file_count(checked)
            effective_path = (
                append_resource_dir(save_path, title)
                if should_force_resource_dir(category_key, file_count)
                else save_path
            )
            target_fid = self._resolve_target_fid(effective_path)
            selected = self._select_items(checked, selection)
            if not selected:
                raise QuarkApiError("分享中没有可转存的文件，或所选文件已失效")
            result = self._post(
                "/1/clouddrive/share/sharepage/save",
                {
                    "fid_list": [item["fid"] for item in selected],
                    "fid_token_list": [item["fid_token"] for item in selected],
                    "to_pdir_fid": target_fid,
                    "pwd_id": checked["pwd_id"],
                    "stoken": checked["stoken"],
                    "pdir_fid": "0",
                    "scene": "link",
                },
                params={
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "app": "clouddrive",
                },
            )
            task_id = str(
                self._find_first_value(
                    result,
                    {"task_id", "taskId", "operation_id", "operationId", "id"},
                )
                or ""
            )
            if not task_id:
                raise QuarkApiError("转存接口未返回任务 ID")
            return ImportResult(
                True,
                JOB_SUBMITTED,
                "夸克转存任务已提交",
                external_task_id=task_id,
                target_path=effective_path,
                raw_data={
                    "provider": "quark",
                    "response": result,
                    "selected_count": len(selected),
                    "selection": selection,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return ImportResult(
                False,
                JOB_FAILED,
                f"夸克转存失败：{exc}",
                target_path=save_path,
            )

    def materialize_resource(
        self,
        *,
        title: str,
        share_url: str,
        root_locator: dict[str, Any],
        landing_path: str,
        password: str = "",
        selection: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ImportResult:
        kind = str((root_locator or {}).get("kind") or "")
        root = str((root_locator or {}).get("value") or "").strip()
        if kind not in {"path", "provider_path"} or not root:
            return ImportResult(
                False,
                JOB_FAILED,
                "夸克来源挂载必须配置 path/provider_path 原生根目录",
            )
        target = self._join_provider_path(root, landing_path)
        return self.import_resource(
            title=title,
            share_url=share_url,
            save_path=target,
            password=password,
            quark_selection=selection,
        )

    def poll_operation(self, receipt: Any) -> dict[str, Any]:
        if not self.cookie:
            return {"status": "failed", "message": self._configuration_message()}
        task_id = str(
            getattr(receipt, "external_ref", "")
            or getattr(receipt, "operation_id", "")
            or ""
        ).strip()
        if not task_id:
            return {"status": "unknown", "message": "夸克任务 ID 缺失"}
        try:
            payload = self._get(
                "/1/clouddrive/task",
                params={
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "task_id": task_id,
                    "retry_index": 0,
                },
            )
            if payload.get("status") not in (None, 0, 200, "0", "200"):
                return {
                    "status": "failed",
                    "message": self._extract_message(payload, "夸克任务查询失败"),
                }
            task = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            state = str(task.get("status") or task.get("task_status") or "").lower()
            if state in {"2", "completed", "success", "done"}:
                return {"status": "completed", "message": "夸克转存完成", "metadata": task}
            if state in {"-1", "3", "failed", "error"}:
                return {
                    "status": "failed",
                    "message": self._extract_message(task, "夸克转存失败"),
                }
            return {"status": "running", "message": "夸克转存进行中", "metadata": task}
        except Exception as exc:  # noqa: BLE001
            return {"status": "running", "message": f"夸克任务暂时无法查询：{exc}"}

    def verify_native_result(self, request: Any) -> dict[str, Any]:
        from ..transfer.verifier import VerifyRequest

        if not isinstance(request, VerifyRequest):
            raise TypeError("verify_native_result requires VerifyRequest")
        receipt = request.operation
        status = self.poll_operation(receipt)
        if status.get("status") == "failed":
            return {"status": "missing", "message": status.get("message") or "夸克转存失败"}
        if status.get("status") != "completed":
            return {"status": "unknown", "message": status.get("message") or "夸克任务未完成"}
        target_path = str((getattr(receipt, "metadata", {}) or {}).get("target_path") or "")
        if not target_path:
            return {"status": "ok", "message": "夸克转存任务已完成"}
        try:
            self._lookup_path_fid(target_path)
            return {"status": "ok", "message": "夸克任务和目标目录均已确认"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unknown", "message": f"夸克目标目录校验失败：{exc}"}

    def cancel_operation(self, receipt: Any) -> dict[str, Any]:
        del receipt
        return {
            "status": "not_cancellable",
            "message": "夸克网页 API 未提供可靠的任务取消接口",
        }

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        base: str | None = None,
    ) -> dict[str, Any]:
        status, payload = self.http.get_json(
            f"{base or self.base_url}{path}",
            params=params or {},
            timeout=self.timeout,
        )
        return self._validate_response(status, payload, path=path)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, value = self.http.post_json(
            f"{self.base_url}{path}",
            payload,
            params=params or {},
            timeout=self.timeout,
        )
        return self._validate_response(status, value, path=path)

    @staticmethod
    def _validate_response(
        status: int,
        payload: Any,
        *,
        path: str = "",
    ) -> dict[str, Any]:
        if status in {401, 403}:
            raise QuarkApiError("夸克 Cookie 已失效")
        if status >= 400:
            raise QuarkApiError(f"夸克 API HTTP {status}")
        if not isinstance(payload, dict):
            raise QuarkApiError("夸克 API 返回格式异常")
        code = payload.get("code")
        if code in (40001, "40001"):
            raise QuarkApiError("夸克 Cookie 已失效，请重新扫码登录")
        # ``10000`` is overloaded: on a ``/share/`` path it means the share is
        # gone, everywhere else it means the cookie died.  Reporting a dead
        # share as an expired cookie sends the operator off to re-scan a QR
        # code that was never the problem.
        if code in (41001, "41001", 41002, "41002", 41003, "41003") or (
            code in (10000, "10000") and "/share/" in str(path or "")
        ):
            raise QuarkApiError("夸克分享不存在或已失效，请重新搜索资源")
        if code in (10000, "10000"):
            raise QuarkApiError("夸克 Cookie 已失效，请重新扫码登录")
        # Quark's account endpoint uses the string ``OK`` for a successful
        # response, while other endpoints commonly use numeric ``0`` or a
        # ``success`` string.  Normalize only the documented success values;
        # unknown business codes must continue to fail closed.
        normalized_code = code.strip().lower() if isinstance(code, str) else code
        if normalized_code not in (None, 0, "0", "200", 200, "success", "ok"):
            raise QuarkApiError(
                str(payload.get("message") or payload.get("msg") or f"夸克 API code={code}")
            )
        return payload

    @staticmethod
    def _detail_params(pwd_id: str, fid: str, stoken: str) -> dict[str, Any]:
        return {
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": fid or "0",
            "force": 0,
            "_page": 1,
            "_size": 50,
            "_fetch_banner": 0,
            "_fetch_share": 1,
            "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
            "ver": 2,
            "fetch_share_full_path": 1,
        }

    def _get_share_detail(
        self,
        pwd_id: str,
        fid: str,
        stoken: str,
        *,
        fetch_share: int = 0,
    ) -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        last: dict[str, Any] = {}
        page = 1
        while True:
            params = self._detail_params(pwd_id, fid, stoken)
            params["_page"] = page
            params["_fetch_share"] = fetch_share
            last = self._get("/1/clouddrive/share/sharepage/detail", params=params)
            rows = self._extract_file_rows(last)
            merged.extend(rows)
            metadata = last.get("metadata") if isinstance(last.get("metadata"), dict) else {}
            total = int(metadata.get("_total") or metadata.get("total") or 0)
            if not rows or (total and len(merged) >= total) or len(rows) < 50:
                break
            page += 1
        data = last.get("data") if isinstance(last.get("data"), dict) else {}
        if isinstance(data, dict) and "list" in data:
            last = {**last, "data": {**data, "list": merged}}
        return last

    def _lookup_path_fid(self, path: str) -> str:
        normalized = self._normalize_provider_path(path)
        payload = self._post(
            "/1/clouddrive/file/info/path_list",
            {"file_path": [normalized], "namespace": "0"},
            params={"pr": "ucpro", "fr": "pc"},
        )
        fid = self._find_first_value(payload.get("data", payload), {"fid", "pdir_fid", "folder_id"})
        if not fid:
            raise QuarkApiError(f"夸克目录不存在：{normalized}")
        return str(fid)

    def _resolve_target_fid(self, path: str) -> str:
        normalized = self._normalize_provider_path(path)
        try:
            return self._lookup_path_fid(normalized)
        except QuarkApiError:
            pass
        payload = self._post(
            "/1/clouddrive/file",
            {
                "pdir_fid": "0",
                "file_name": "",
                "dir_path": normalized,
                "dir_init_lock": False,
            },
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        )
        fid = self._find_first_value(payload.get("data", payload), {"fid", "pdir_fid", "folder_id", "id"})
        if not fid:
            return self._lookup_path_fid(normalized)
        return str(fid)

    def _select_items(
        self,
        checked: dict[str, Any],
        selection: dict[str, Any],
    ) -> list[dict[str, str]]:
        rows = self._extract_file_rows(checked)
        mode = str(selection.get("mode") or "all")
        selected_ids: set[str] = set()
        if mode == "root_dirs":
            selected_ids = self._selection_ids(selection.get("selected_dirs"))
            rows = [row for row in rows if self._file_item(row)["is_dir"]]
        elif mode == "root_items":
            selected_ids = self._selection_ids(selection.get("selected_items"))
            rows = [row for row in rows if not self._file_item(row)["is_dir"]]
        elif mode == "subdir_items":
            base = selection.get("base_dir") if isinstance(selection.get("base_dir"), dict) else {}
            base_fid = str(base.get("fid") or "")
            if not base_fid:
                raise QuarkApiError("未指定要展开的夸克文件夹")
            listing = self.list_files(checked["pwd_id"], base_fid, checked["stoken"])
            if listing.get("success") is False:
                raise QuarkApiError(str(listing.get("message") or "夸克子目录读取失败"))
            rows = self._select_subdir_rows(
                checked,
                base_fid,
                self._extract_file_rows(listing),
                selection.get("selected_items"),
            )
            selected_ids = set()
        if selected_ids:
            rows = [row for row in rows if str(self._file_item(row)["fid"]) in selected_ids]
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            item = self._file_item(row)
            fid = str(item.get("fid") or "")
            token = str(
                self._find_first_value(
                    row,
                    {"share_fid_token", "fid_token", "fidToken", "shareFidToken"},
                )
                or ""
            )
            if fid and token and fid not in seen:
                seen.add(fid)
                result.append({"fid": fid, "fid_token": token})
        return result

    @staticmethod
    def _selection_ids(value: Any) -> set[str]:
        return {
            str(item.get("fid") or "")
            for item in (value or [])
            if isinstance(item, dict) and str(item.get("fid") or "")
        }

    def _select_subdir_rows(
        self,
        checked: dict[str, Any],
        base_fid: str,
        base_rows: list[dict[str, Any]],
        selected_items: Any,
    ) -> list[dict[str, Any]]:
        """把 subdir_items 的选中项解析为真实文件行。

        - 带相对 path 的条目（支持多级目录）按 path 从 base 逐级查找。
        - 无 path 的条目回退到 base 直接子项按 fid 匹配（保持旧行为）。
        """
        selected = [
            item
            for item in (selected_items or [])
            if isinstance(item, dict) and str(item.get("fid") or "")
        ]
        if not selected:
            return []
        by_id = {str(self._file_item(row)["fid"]): row for row in base_rows}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in selected:
            fid = str(item.get("fid") or "")
            path = str(item.get("path") or "").strip().strip("/")
            if path and "/" in path:
                target = self._walk_to_fid(checked["pwd_id"], checked["stoken"], base_fid, path)
                if target is not None:
                    target_fid = str(self._file_item(target)["fid"] or "")
                    if target_fid and target_fid not in seen:
                        seen.add(target_fid)
                        result.append(target)
                continue
            row = by_id.get(fid)
            if row is not None and fid not in seen:
                seen.add(fid)
                result.append(row)
        return result

    def _walk_to_fid(
        self,
        pwd_id: str,
        stoken: str,
        base_fid: str,
        path: str,
    ) -> dict[str, Any] | None:
        """从 base 目录按相对 path 逐级解析到目标文件行，支持任意层级。"""
        current = str(base_fid or "")
        current_row: dict[str, Any] | None = None
        for segment in str(path or "").split("/"):
            segment = str(segment or "").strip()
            if not segment:
                continue
            if not current:
                return None
            listing = self.list_files(pwd_id, current, stoken)
            if listing.get("success") is not True:
                return None
            child = next(
                (
                    row
                    for row in self._extract_file_rows(listing)
                    if str(self._file_item(row).get("name") or "").strip() == segment
                ),
                None,
            )
            if child is None:
                return None
            current_row = child
            current = str(self._file_item(child).get("fid") or "")
            if not current:
                return None
        return current_row

    @staticmethod
    def _normalize_selection(selection: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(selection, dict):
            return {"mode": "all"}
        mode = str(selection.get("mode") or "all")
        if mode not in {"all", "root_dirs", "root_items", "subdir_items"}:
            mode = "all"
        return {**selection, "mode": mode}

    @classmethod
    def _extract_file_count(cls, payload: Any) -> int:
        value = cls._find_first_value(payload, {"file_num", "file_count", "fileCount", "total"})
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return len(cls._extract_file_rows(payload))

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
        elif isinstance(payload, list):
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
    def _file_item(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            item = {}
        fid = str(
            item.get("fid")
            or item.get("file_id")
            or item.get("fileId")
            or item.get("id")
            or ""
        )
        name = str(
            item.get("file_name")
            or item.get("fileName")
            or item.get("name")
            or item.get("title")
            or fid
            or "未命名文件"
        )
        kind = str(item.get("file_type") or item.get("fileType") or item.get("type") or "").lower()
        dir_value = item.get("dir")
        is_dir = bool(
            dir_value in (True, 1, "1")
            or item.get("is_dir") in (True, 1, "1")
            or item.get("isDir") in (True, 1, "1")
            or kind in {"0", "folder", "dir", "directory"}
        )
        return {"fid": fid, "name": name, "is_dir": is_dir}

    @staticmethod
    def _extract_message(payload: Any, default: str) -> str:
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("msg") or payload.get("error") or default)
        return default

    @staticmethod
    def _extract_pwd_id(url: str) -> str:
        text = str(url or "").strip()
        parsed = urlparse(text if "://" in text else f"https://{text}")
        query = parse_qs(parsed.query)
        direct = str((query.get("pwd_id") or query.get("pwdId") or [""])[0])
        if direct:
            return direct
        parts = [part for part in parsed.path.split("/") if part]
        try:
            index = parts.index("s")
        except ValueError:
            return ""
        return parts[index + 1] if index + 1 < len(parts) else ""

    @staticmethod
    def _normalize_provider_path(path: str) -> str:
        return "/" + "/".join(
            part
            for part in str(path or "").replace("\\", "/").split("/")
            if part not in {"", "."}
        )

    @classmethod
    def _join_provider_path(cls, root: str, child: str) -> str:
        return cls._normalize_provider_path(
            "/".join(part.strip("/") for part in (root, child) if str(part or "").strip("/"))
        )


__all__ = ["QuarkApiError", "QuarkImporter"]
