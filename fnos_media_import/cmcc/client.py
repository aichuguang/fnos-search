from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .auth import CmccAuthConfigError, CmccAuthExpired, CmccAuthProvider


class CmccApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status_code: int | None = None,
        payload: Any = None,
        auth_expired: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload
        self.auth_expired = auth_expired


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _json_for_sign(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _format_datetime_cst() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _random_str(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _get_new_sign_hash(payload: dict[str, Any], datetime_text: str, random_text: str) -> str:
    # OpenList drivers/139/util.go calSign:
    # encodeURIComponent(JSON(body)) -> split chars -> sort -> base64 -> md5,
    # then md5(datetime:random) and md5 both parts.
    encoded = quote(_json_for_sign(dict(payload)), safe="-_.!~*'()")
    sorted_text = "".join(sorted(encoded))
    first = _md5(base64.b64encode(sorted_text.encode("utf-8")).decode("ascii"))
    second = _md5(f"{datetime_text}:{random_text}")
    return _md5(first + second).upper()


def _split_cloud_path(path: str) -> list[str]:
    text = str(path or "").replace("\\", "/").strip("/")
    if not text or text == ".":
        return []
    norm = posixpath.normpath(text)
    if norm in {"", "."} or norm == ".." or norm.startswith("../"):
        return []
    return [part for part in norm.split("/") if part and part != "."]


class CmccClient:
    """139 personal_new WebAPI client, aligned with OpenList drivers/139."""

    RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

    OPENLIST_PERSONAL_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Caller": "web",
        "Cms-Device": "default",
        "Mcloud-Channel": "1000101",
        "Mcloud-Client": "10701",
        "Mcloud-Route": "001",
        "Mcloud-Version": "7.14.0",
        "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
        "x-huawei-channelSrc": "10000034",
        "x-inner-ntwk": "2",
        "x-m4c-caller": "PC",
        "x-m4c-src": "10002",
        "x-SvcType": "1",
        "X-Yun-Api-Version": "v1",
        "X-Yun-App-Channel": "10000034",
        "X-Yun-Channel-Source": "10000034",
        "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
        "X-Yun-Module-Type": "100",
        "X-Yun-Svc-Type": "1",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def __init__(self, auth: CmccAuthProvider | None = None, *, timeout: int = 30) -> None:
        self.auth = auth or CmccAuthProvider()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def host(self) -> str:
        host = self.auth.host.rstrip("/")
        if host == "https://personal-kd-njs.yun.139.com":
            host = "https://personal-kd-njs.yun.139.com/hcy"
        return host

    @property
    def phone(self) -> str:
        return self.auth.get_credentials().phone

    def _account(self) -> dict[str, Any]:
        if not self.phone:
            raise CmccAuthConfigError("CMCC WebAPI 需要手机号；Basic 里解析不到时请配置 CMCC_PHONE=你的139手机号")
        return {"account": self.phone, "accountType": 1}

    @staticmethod
    def _normalize_path(path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return "/"
        # OpenList 的 personal host 已经包含 /hcy，接口路径是 /file/xxx。
        if text.startswith("/hcy/"):
            return text[4:]
        if not text.startswith("/"):
            return "/" + text
        return text

    def _compute_mcloud_sign(self, payload: dict[str, Any]) -> str:
        datetime_text = _format_datetime_cst()
        random_text = _random_str(16)
        return f"{datetime_text},{random_text},{_get_new_sign_hash(payload, datetime_text, random_text)}"

    def _headers(self, payload: dict[str, Any]) -> dict[str, str]:
        headers = dict(self.OPENLIST_PERSONAL_HEADERS)
        headers.update(self.auth.get_headers())
        headers["Mcloud-Sign"] = self._compute_mcloud_sign(payload)
        return headers

    def _share_headers(self, has_json: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Caller": "web",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Version": "7.17.2",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "x-m4c-caller": "PC",
        }
        if has_json:
            headers["Content-Type"] = "application/json"
        headers.update(self.auth.get_headers())
        return headers

    @staticmethod
    def extract_share_url(value: str) -> tuple[str, str, str]:
        """Return (link_id, password, pdir_fid) from a 139 share URL or raw link id."""
        raw = str(value or "").strip()
        if not raw:
            return "", "", "root"
        text = raw.replace("：", ":").replace("？", "?").replace("（", "(").replace("）", ")")
        compact = re.sub(r"\s+", "", text)
        try:
            compact = unquote(compact)
        except Exception:
            pass
        password = ""
        for pattern in (
            r"(?:passwd|pwd|password)=([A-Za-z0-9]{4,8})",
            r"(?:密码|提取码|访问码)[:：]?\s*([A-Za-z0-9]{4,8})",
            r"\(([A-Za-z0-9]{4,8})\)",
        ):
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if match:
                password = str(match.group(1) or "").strip()
                break
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", raw) and "/" not in raw:
            return raw, password, "root"
        url_match = re.search(r"(https?://(?:yun|caiyun)\.139\.com[^\s]*)", text, flags=re.IGNORECASE)
        extracted_url = url_match.group(1) if url_match else text
        if not re.match(r"^https?://", extracted_url, flags=re.IGNORECASE) and re.search(r"(?:yun|caiyun)\.139\.com", extracted_url, flags=re.IGNORECASE):
            extracted_url = "https://" + extracted_url.lstrip("/")
        try:
            parsed = urlparse(extracted_url)
        except Exception:
            return "", password, "root"
        query = parse_qs(parsed.query or "")
        fragment = str(parsed.fragment or "")
        frag_path, frag_query = (fragment.split("?", 1) + [""])[:2] if fragment else ("", "")
        frag_qs = parse_qs(frag_query or "")
        pdir_fid = "root"
        for source in (query, frag_qs):
            fid = str((source.get("fid") or source.get("pCaID") or [""])[0] or "").strip()
            if fid and fid not in {"0", "root"}:
                pdir_fid = fid
                break
        if frag_path:
            match = re.search(r"(?:^|/)list/share/([^/?#]+)", frag_path)
            if match and match.group(1):
                pdir_fid = str(match.group(1)).strip()
        link_id = str((query.get("linkID") or query.get("linkId") or [""])[0] or "").strip()
        if not link_id and parsed.query and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", parsed.query):
            link_id = parsed.query
        if not link_id:
            link_id = str((frag_qs.get("linkID") or frag_qs.get("linkId") or [""])[0] or "").strip()
        if not link_id and frag_path:
            candidate = re.split(r"[/?]", frag_path)[-1]
            if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", candidate or ""):
                link_id = candidate
        if not link_id:
            match = re.search(r"linkID=([A-Za-z0-9_-]{4,64})", compact, flags=re.IGNORECASE)
            if match:
                link_id = match.group(1)
        return link_id, password, pdir_fid

    def share_post_json(self, path: str, payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        url = f"https://share-kd-njs.yun.139.com/{str(path or '').lstrip('/')}"
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=self._share_headers(True),
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise CmccApiError(f"CMCC share request failed: {path}: {exc}") from exc
        if response.status_code != 200:
            raise CmccApiError(self._response_message(response), status_code=response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise CmccApiError(f"CMCC share response is not JSON: {path}") from exc
        if self.auth.is_auth_error(data):
            raise CmccAuthExpired(self._payload_message(data))
        code = str(data.get("code") or "").strip()
        success = data.get("success")
        if code not in {"", "0", "0000", "200"} and success is not True:
            raise CmccApiError(self._payload_message(data), code=code, payload=data)
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        return body if isinstance(body, dict) else {"value": body}

    def get_share_info(self, link_id: str, *, password: str = "", pdir_fid: str = "root", start_num: int = 1, end_num: int = 200) -> dict[str, Any]:
        payload = {
            "getOutLinkInfoReq": {
                "account": self.phone or "",
                "linkID": str(link_id or ""),
                "passwd": password or "",
                "pCaID": pdir_fid or "root",
                "caSrt": 0,
                "coSrt": 0,
                "srtDr": 1,
                "bNum": int(start_num),
                "eNum": int(end_num),
            }
        }
        if not payload["getOutLinkInfoReq"]["linkID"]:
            raise CmccApiError("139 share linkID is required")
        return self.share_post_json("/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6", payload)

    def list_share_dir(self, link_id: str, *, password: str = "", pdir_fid: str = "root", page_size: int = 200) -> list[dict[str, Any]]:
        info = self.get_share_info(link_id, password=password, pdir_fid=pdir_fid or "root", start_num=1, end_num=page_size)
        return self._normalize_share_items(info, pdir_fid=pdir_fid or "root")

    def save_share_items(
        self,
        *,
        link_id: str,
        target_parent_file_id: str,
        content_paths: list[str] | None = None,
        catalog_paths: list[str] | None = None,
        need_password: bool = False,
        password: str = "",
    ) -> dict[str, Any]:
        payload = {
            "createOuterLinkBatchOprTaskReq": {
                "msisdn": self.phone or "",
                "ownerAccount": "",
                "taskType": 1,
                "linkID": str(link_id or ""),
                "passwd": password or "",
                "needPassword": bool(need_password),
                "taskInfo": {
                    "linkID": str(link_id or ""),
                    "passwd": password or "",
                    "needPassword": bool(need_password),
                    "contentInfoList": [str(item) for item in (content_paths or []) if str(item or "").strip()],
                    "catalogInfoList": [str(item) for item in (catalog_paths or []) if str(item or "").strip()],
                    "newCatalogID": "/" if str(target_parent_file_id or "") in {"", "0", "root"} else str(target_parent_file_id),
                },
            }
        }
        if not payload["createOuterLinkBatchOprTaskReq"]["taskInfo"]["contentInfoList"] and not payload["createOuterLinkBatchOprTaskReq"]["taskInfo"]["catalogInfoList"]:
            raise CmccApiError("139 native save requires at least one file or folder path")
        return self.share_post_json("/yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask", payload)

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not str(task_id or "").strip():
            return {}
        result = self.post_json("/task/get", {"taskId": str(task_id)}, retries=1)
        return self.require_success(result, action="get task")

    @staticmethod
    def _normalize_share_items(info: dict[str, Any], *, pdir_fid: str = "root") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for folder in info.get("caLst") or []:
            if not isinstance(folder, dict):
                continue
            fid = str(folder.get("catalogID") or folder.get("caID") or "").strip()
            name = str(folder.get("catalogName") or folder.get("caName") or fid).strip()
            if not fid:
                continue
            result.append(
                {
                    "fid": fid,
                    "id": fid,
                    "name": name,
                    "fileName": name,
                    "type": "dir",
                    "isFolder": True,
                    "dir": True,
                    "path": str(folder.get("path") or fid),
                    "share_fid_token": json.dumps({"path": str(folder.get("path") or fid), "pid": str(pdir_fid or "root"), "dir": 1, "name": name}, ensure_ascii=False),
                    "raw": folder,
                }
            )
        for file_item in info.get("coLst") or []:
            if not isinstance(file_item, dict):
                continue
            fid = str(file_item.get("contentID") or file_item.get("coID") or "").strip()
            name = str(file_item.get("contentName") or file_item.get("coName") or fid).strip()
            if not fid:
                continue
            result.append(
                {
                    "fid": fid,
                    "id": fid,
                    "name": name,
                    "fileName": name,
                    "type": "file",
                    "isFolder": False,
                    "dir": False,
                    "size": file_item.get("coSize") or file_item.get("contentSize") or file_item.get("size"),
                    "path": str(file_item.get("path") or fid),
                    "share_fid_token": json.dumps({"path": str(file_item.get("path") or fid), "pid": str(pdir_fid or "root"), "dir": 0, "name": name}, ensure_ascii=False),
                    "raw": file_item,
                }
            )
        return result

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        retries: int = 1,
        timeout: int | None = None,
        sign_catalog_id: str = "",
    ) -> dict[str, Any]:
        # sign_catalog_id 保留兼容旧调用；OpenList 实现是按真实 body 签名。
        _ = sign_catalog_id
        last_error: Exception | None = None
        for reload_attempt in range(2):
            for attempt in range(retries + 1):
                try:
                    return self._post_json_once(path, payload, timeout=timeout or self.timeout)
                except CmccAuthExpired as exc:
                    last_error = exc
                    if reload_attempt == 0:
                        break
                    raise
                except CmccApiError as exc:
                    last_error = exc
                    if exc.auth_expired and reload_attempt == 0:
                        break
                    if exc.status_code in self.RETRYABLE_HTTP_STATUS and attempt < retries:
                        time.sleep(1 + attempt)
                        continue
                    raise
                except requests.RequestException as exc:
                    last_error = exc
                    if attempt < retries:
                        time.sleep(1 + attempt)
                        continue
                    raise CmccApiError(f"CMCC WebAPI request failed: {path}: {exc}") from exc
            self.auth.reload()
        if isinstance(last_error, CmccApiError):
            raise last_error
        if isinstance(last_error, CmccAuthExpired):
            raise last_error
        raise CmccApiError(f"CMCC WebAPI request failed: {path}: {last_error}")

    def _post_json_once(self, path: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        api_path = self._normalize_path(path)
        url = f"{self.host}{api_path}"
        # 签名与实际发送 body 必须使用同一份 JSON 文本。
        body_text = _json_for_sign(payload)
        response = self.session.post(
            url,
            data=body_text.encode("utf-8"),
            headers=self._headers(payload),
            timeout=timeout,
        )
        if response.status_code != 200:
            message = self._response_message(response)
            auth_expired = self.auth.is_auth_error(status_code=response.status_code, message=message)
            if auth_expired:
                raise CmccAuthExpired(message)
            raise CmccApiError(message, status_code=response.status_code, auth_expired=auth_expired)
        try:
            data = response.json()
        except ValueError as exc:
            raise CmccApiError(f"CMCC WebAPI response is not JSON: {api_path}") from exc
        if self.auth.is_auth_error(data):
            raise CmccAuthExpired(self._payload_message(data))
        return data if isinstance(data, dict) else {"data": data}

    def require_success(self, result: dict[str, Any], *, action: str) -> dict[str, Any]:
        code = str(result.get("code") or "").strip()
        success = result.get("success")
        if success is True or code in {"0000", "0"} or (not code and "data" in result):
            data = result.get("data")
            if isinstance(data, dict):
                return data
            if data is None:
                return result
            return {"value": data}
        message = self._payload_message(result) or f"{action} failed"
        if self._is_auth_config_message(message):
            raise CmccAuthConfigError(message)
        auth_expired = self.auth.is_auth_error(result, message=message)
        if auth_expired:
            raise CmccAuthExpired(message)
        raise CmccApiError(message, code=code, payload=result, auth_expired=auth_expired)

    @staticmethod
    def _is_auth_config_message(message: str) -> bool:
        text = str(message or "").strip().lower()
        return any(
            token in text
            for token in (
                "authorization",
                "basic",
                "missing header",
                "required header",
                "请求头必要参数为空",
                "必要参数为空",
            )
        )

    def create_file(
        self,
        *,
        name: str,
        size: int,
        parent_file_id: str = "",
        parent_path: str = "",
        content_hash: str = "",
        rename_mode: str = "auto_rename",
        part_infos: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        create_path, payload, parent_id = self.build_create_file_request(
            name=name,
            size=size,
            parent_file_id=parent_file_id,
            parent_path=parent_path,
            content_hash=content_hash,
            rename_mode=rename_mode,
            part_infos=part_infos,
        )
        result = self.post_json(create_path, payload, retries=1)
        data = self.require_success(result, action="create file")
        if parent_id and "parentFileId" not in data:
            data["parentFileId"] = parent_id
        return data

    def build_create_file_request(
        self,
        *,
        name: str,
        size: int,
        parent_file_id: str = "",
        parent_path: str = "",
        content_hash: str = "",
        rename_mode: str = "auto_rename",
        part_infos: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        parent_id = str(parent_file_id or "").strip() or self.ensure_path(parent_path)
        if not parent_id:
            raise CmccApiError("target parent file id/path is required")

        if not part_infos:
            part_infos = [
                {
                    "partNumber": 1,
                    "partSize": int(size),
                    "parallelHashCtx": {"partOffset": 0},
                }
            ]
        payload: dict[str, Any] = {
            "contentHash": content_hash,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": part_infos,
            "size": int(size),
            "parentFileId": parent_id,
            "name": name,
            "type": "file",
            "fileRenameMode": rename_mode or "auto_rename",
        }
        if not content_hash:
            payload.pop("contentHash", None)
            payload.pop("contentHashAlgorithm", None)
        return os.getenv("CMCC_WEB_CREATE_PATH", "/file/create"), payload, parent_id

    def get_upload_urls(self, *, file_id: str, upload_id: str, part_infos: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "fileId": file_id,
            "uploadId": upload_id,
            "partInfos": part_infos,
            "commonAccountInfo": self._account(),
        }
        result = self.post_json(os.getenv("CMCC_WEB_GET_UPLOAD_URL_PATH", "/file/getUploadUrl"), payload, retries=1)
        return self.require_success(result, action="get upload url")

    def redacted_headers_for_debug(self, *, payload: dict[str, Any] | None = None, sign_catalog_id: str = "") -> dict[str, str]:
        _ = sign_catalog_id
        headers = self._headers(payload or {})
        authorization = headers.get("Authorization") or ""
        if authorization:
            if authorization.lower().startswith("basic "):
                token = authorization.split(" ", 1)[1] if " " in authorization else ""
                headers["Authorization"] = f"Basic ***({len(token)} chars)"
            else:
                headers["Authorization"] = "***"
        if headers.get("Mcloud-Sign"):
            parts = headers["Mcloud-Sign"].split(",")
            if len(parts) == 3:
                headers["Mcloud-Sign"] = f"{parts[0]},{parts[1]},***"
        return headers

    def complete_file(self, *, upload_id: str, file_id: str, content_hash: str, parent_file_id: str = "") -> dict[str, Any]:
        _ = parent_file_id
        if not upload_id or not file_id:
            return {}
        payload = {
            "contentHash": content_hash,
            "contentHashAlgorithm": "SHA256",
            "fileId": file_id,
            "uploadId": upload_id,
        }
        result = self.post_json(os.getenv("CMCC_WEB_COMPLETE_PATH", "/file/complete"), payload, retries=0)
        return self.require_success(result, action="complete file")

    def create_folder(self, *, name: str, parent_file_id: str) -> dict[str, Any]:
        parent_id = str(parent_file_id or "/").strip() or "/"
        payload = {
            "parentFileId": parent_id,
            "name": name,
            "description": "",
            "type": "folder",
            "fileRenameMode": "force_rename",
        }
        result = self.post_json(os.getenv("CMCC_WEB_CREATE_PATH", "/file/create"), payload, retries=1)
        data = self.require_success(result, action="create folder")
        if parent_id and "parentFileId" not in data:
            data["parentFileId"] = parent_id
        return data

    def list_dir(self, parent_file_id: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        parent_id = str(parent_file_id or "/").strip() or "/"
        cursor: Any = ""
        items: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        for _ in range(100):
            cursor_key = str(cursor or "__FIRST__")
            if cursor_key in seen_cursors:
                break
            seen_cursors.add(cursor_key)
            payload = {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "pageInfo": {"pageCursor": cursor, "pageSize": int(page_size)},
                "parentFileId": parent_id,
            }
            result = self.post_json("/file/list", payload, retries=1)
            data = self.require_success(result, action="list dir")
            rows = data.get("items") or data.get("fileList") or []
            if isinstance(rows, list):
                items.extend(row for row in rows if isinstance(row, dict))
            cursor = data.get("nextPageCursor") or (data.get("pageInfo") or {}).get("nextPageCursor") or ""
            if not cursor:
                break
        return items

    def check_exists(self, *, parent_file_id: str, file_name: str) -> dict[str, Any]:
        target = str(file_name or "").strip()
        if not target:
            return {}
        for item in self.list_dir(parent_file_id):
            if str(item.get("name") or item.get("fileName") or "").strip() == target:
                return self._normalize_exists(item)
        return {"exist": False, "file_id": "", "file_type": "", "size": None, "raw": {}}

    def ensure_path(self, cloud_path: str) -> str:
        current = "/"
        for segment in _split_cloud_path(cloud_path):
            exists = self.check_exists(parent_file_id=current, file_name=segment)
            if exists.get("exist") and exists.get("file_id"):
                file_type = str(exists.get("file_type") or "").lower()
                if file_type and "folder" not in file_type and "dir" not in file_type:
                    raise CmccApiError(f"target path segment exists but is not a folder: {segment}")
                current = str(exists["file_id"])
                continue
            created = self.create_folder(name=segment, parent_file_id=current)
            current = str(created.get("fileId") or created.get("file_id") or created.get("id") or "")
            if not current:
                raise CmccApiError(f"create folder succeeded but no fileId returned: {segment}")
        return current

    @staticmethod
    def _normalize_exists(data: dict[str, Any]) -> dict[str, Any]:
        file_id = (
            data.get("fileId")
            or data.get("file_id")
            or data.get("id")
            or data.get("contentID")
            or data.get("contentId")
            or ""
        )
        raw_type = str(data.get("type") or data.get("fileType") or data.get("category") or "").lower()
        file_type = "folder" if raw_type == "folder" else (raw_type or "file")
        size = data.get("size") or data.get("fileSize") or data.get("contentSize")
        try:
            size_int = int(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size_int = None
        return {
            "exist": True,
            "file_id": str(file_id or ""),
            "file_type": file_type,
            "size": size_int,
            "raw": data,
        }

    @staticmethod
    def _payload_message(payload: dict[str, Any]) -> str:
        for key in ("message", "msg", "desc", "error", "errorMsg"):
            value = payload.get(key)
            if value:
                return str(value)
        return json.dumps(payload, ensure_ascii=False)[:300]

    @staticmethod
    def _response_message(response: requests.Response) -> str:
        text = (response.text or "").strip()
        if not text:
            return f"CMCC WebAPI HTTP {response.status_code}"
        try:
            data = response.json()
            if isinstance(data, dict):
                return CmccClient._payload_message(data)
        except ValueError:
            pass
        return f"CMCC WebAPI HTTP {response.status_code}: {text[:300]}"
