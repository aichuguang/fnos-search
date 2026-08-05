from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass
from typing import Any

import requests


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".rmvb",
    ".ts",
    ".m2ts",
    ".iso",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    # 139 等网盘常把每一集以未压缩 tar 包保存；与其它影视压缩包一样，
    # Organizer 需要先识别并按集号整理，后续是否解包由用户/媒体端决定。
    ".tar",
    # 影视资源常见压缩包：用户会自行解压，整理时仍按影视文件参与 SxxEyy 命名。
    ".zip",
    ".rar",
    ".7z",
}


class OpenListError(RuntimeError):
    pass


class OpenListEndpointUnsupported(OpenListError):
    """表示当前 OpenList 版本不支持请求的 API 端点。"""


class OpenListTransientError(OpenListError):
    """表示 OpenList 请求遭遇可重试的瞬时网络或服务繁忙故障。"""


def normalize_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return "/"
    if "://" in text:
        # OpenList fs APIs only accept virtual paths, not URLs.
        raise OpenListError("OpenList 路径不能是 URL")
    text = "/" + text.strip("/")
    norm = posixpath.normpath(text)
    if norm in {"", "."}:
        return "/"
    if norm.startswith("/../") or norm == "/..":
        raise OpenListError("OpenList 路径不能包含越级目录")
    return norm


def join_path(*parts: Any) -> str:
    cleaned = [str(part or "").replace("\\", "/").strip("/") for part in parts if str(part or "").strip("/")]
    return normalize_path("/".join(cleaned))


def dirname(value: Any) -> str:
    path = normalize_path(value)
    parent = posixpath.dirname(path.rstrip("/"))
    return parent or "/"


def basename(value: Any) -> str:
    return posixpath.basename(normalize_path(value).rstrip("/"))


@dataclass
class OpenListItem:
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: str = ""
    raw: dict[str, Any] | None = None


class OpenListClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self.token = str(config.get("token") or "").strip()
        self.timeout = int(config.get("timeout") or 30)
        self.batch_timeout = _bounded_int(config.get("batch_timeout"), default=600, minimum=30, maximum=3600)
        self.verify_tls = _as_bool(config.get("verify_tls"), True)
        self.list_refresh_default = _as_bool(config.get("list_refresh_default"), False)
        self.use_env_proxy = _as_bool(config.get("use_env_proxy"), False)
        self.request_retries = _bounded_int(config.get("request_retries"), default=3, minimum=0, maximum=8)
        self.retry_backoff_seconds = _bounded_float(config.get("retry_backoff_seconds"), default=0.5, minimum=0.0, maximum=10.0)
        self._unsupported_endpoints: set[str] = set()
        self.session = requests.Session()
        # requests.Session 默认会读取 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
        # OpenList 通常部署在本机、Docker 网关或内网 NAS 上，误走本机代理
        # （例如 127.0.0.1:7890）会导致内网 API 超时；默认显式关闭环境代理。
        self.session.trust_env = self.use_env_proxy

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def test(self) -> dict[str, Any]:
        if not self.base_url:
            return {"success": False, "message": "OpenList 地址为空"}
        try:
            data = self._request("GET", "/api/me")
            return {"success": True, "message": "OpenList 连接正常", "raw": data}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": str(exc)}

    def list_dir(self, path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
        req_path = normalize_path(path)
        payload = {
            "path": req_path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": self.list_refresh_default if refresh is None else bool(refresh),
        }
        data = self._request("POST", "/api/fs/list", json=payload)
        rows = ((data.get("data") or {}).get("content") or []) if isinstance(data, dict) else []
        result: list[OpenListItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            result.append(
                OpenListItem(
                    name=name,
                    path=join_path(req_path, name),
                    is_dir=bool(row.get("is_dir")),
                    size=_safe_int(row.get("size")),
                    modified=str(row.get("modified") or row.get("created") or ""),
                    raw=row,
                )
            )
        return result

    def scan_videos(
        self,
        root_path: str,
        *,
        max_depth: int = 8,
        max_files: int = 500,
        refresh: bool = False,
        expected_names: list[str] | tuple[str, ...] | set[str] | None = None,
        expected_paths: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[OpenListItem]:
        root = normalize_path(root_path)
        result: list[OpenListItem] = []
        visited: set[str] = set()
        file_limit = max(0, int(max_files or 0))
        wanted_names = {str(item or "").strip().casefold() for item in (expected_names or []) if str(item or "").strip()}
        found_names: set[str] = set()
        inspected_video_files = 0

        def limit_reached() -> bool:
            return bool(file_limit and inspected_video_files >= file_limit)

        if refresh and root != "/":
            # 外部云盘 API 新建目录后，OpenList 需要先刷新父目录才能识别新对象。
            self.list_dir(dirname(root), refresh=True)

        # rclone 回调能够提供本次上传文件的完整目标路径。优先按精确路径读取，
        # 避免多个文件直接落在分类根时递归遍历整个电视剧/电影目录。
        scoped_paths = _scoped_expected_video_paths(root, expected_paths)
        if scoped_paths:
            grouped: dict[str, set[str]] = {}
            for path in scoped_paths:
                grouped.setdefault(dirname(path), set()).add(basename(path).casefold())
            found_by_path: dict[str, OpenListItem] = {}
            for parent, names in grouped.items():
                for item in self.list_dir(parent, refresh=refresh):
                    if item.is_dir or item.name.casefold() not in names:
                        continue
                    if posixpath.splitext(item.name)[1].lower() not in VIDEO_EXTENSIONS:
                        continue
                    found_by_path[normalize_path(item.path).casefold()] = item
            # OpenList/云盘可能仍在落盘。只要有一个精确目标尚不可见，就返回空结果，
            # 由 Organizer 的可见性退避机制稍后整批重试，不能先整理半批文件。
            if any(path.casefold() not in found_by_path for path in scoped_paths):
                return []
            return [found_by_path[path.casefold()] for path in scoped_paths]

        def walk(path: str, depth: int) -> None:
            nonlocal inspected_video_files
            if limit_reached() or depth > max_depth or (wanted_names and found_names >= wanted_names):
                return
            normalized = normalize_path(path)
            if normalized in visited:
                return
            visited.add(normalized)
            # refresh=True 只强制刷新扫描根目录。若对整个分类树的每个子目录
            # 都传 refresh=true，OpenList 会连续触发底层云盘刷新，容易导致对端重置连接。
            items = self.list_dir(normalized, refresh=refresh and depth == 0)
            # 先处理当前目录文件，再递归子目录。直接保存到分类根的单集文件
            # 可以立即命中 expected_names，避免为了一个新文件扫描整棵媒体树。
            for item in items:
                if limit_reached():
                    return
                if item.is_dir or posixpath.splitext(item.name)[1].lower() not in VIDEO_EXTENSIONS:
                    continue
                normalized_name = item.name.casefold()
                if wanted_names and normalized_name not in wanted_names:
                    continue
                inspected_video_files += 1
                result.append(item)
                if normalized_name in wanted_names:
                    found_names.add(normalized_name)
            if wanted_names and found_names >= wanted_names:
                return
            for item in items:
                if limit_reached() or (wanted_names and found_names >= wanted_names):
                    return
                if not item.is_dir or item.name in {"@eaDir", "#recycle", ".Trash", "System Volume Information"}:
                    continue
                walk(item.path, depth + 1)

        walk(root, 0)
        return result

    def get_item(self, path: str) -> OpenListItem | None:
        normalized = normalize_path(path)
        if normalized == "/":
            return OpenListItem(name="/", path="/", is_dir=True)
        parent = dirname(normalized)
        name = basename(normalized)
        try:
            return next((item for item in self.list_dir(parent) if item.name == name), None)
        except OpenListTransientError:
            # 瞬时故障不能伪装成“文件不存在”。写操作依赖 exists() 做前置
            # 判断时必须停止，让上层退避或转人工重试，避免在服务繁忙期间
            # 继续发起 mkdir/rename/move/remove。
            raise
        except Exception:
            return None

    def exists(self, path: str) -> bool:
        return self.get_item(path) is not None

    def mkdir(self, path: str) -> bool:
        data = self._request("POST", "/api/fs/mkdir", json={"path": normalize_path(path)})
        return _ok(data)

    def rename(self, source_path: str, new_name: str, *, overwrite: bool = False) -> bool:
        data = self._request(
            "POST",
            "/api/fs/rename",
            json={"path": normalize_path(source_path), "name": str(new_name or "").strip(), "overwrite": bool(overwrite)},
        )
        return _ok(data)

    def batch_rename(
        self,
        source_dir: str,
        renames: list[tuple[str, str]],
        timeout: int | float | None = None,
    ) -> bool:
        """一次请求精确重命名同一目录下的多个文件。"""

        objects = [
            {"src_name": str(src or "").strip(), "new_name": str(dst or "").strip()}
            for src, dst in renames
            if str(src or "").strip() and str(dst or "").strip()
        ]
        if not objects:
            return True
        data = self._bulk_request(
            "/api/fs/batch_rename",
            json={"src_dir": normalize_path(source_dir), "rename_objects": objects},
            timeout=timeout,
        )
        return _ok(data)

    def regex_rename(
        self,
        source_dir: str,
        source_regex: str,
        replacement: str,
        timeout: int | float | None = None,
    ) -> bool:
        """一次请求按 Go/RE2 正则重命名同一目录下的文件。"""

        pattern = str(source_regex or "").strip()
        if not pattern:
            raise OpenListError("正则重命名匹配表达式为空")
        data = self._bulk_request(
            "/api/fs/regex_rename",
            json={
                "src_dir": normalize_path(source_dir),
                "src_name_regex": pattern,
                "new_name_regex": str(replacement or ""),
            },
            timeout=timeout,
        )
        return _ok(data)

    def move(self, source_path: str, target_dir: str, *, overwrite: bool = False, skip_existing: bool = True, merge: bool = True) -> bool:
        src = normalize_path(source_path)
        data = self._request(
            "POST",
            "/api/fs/move",
            json={
                "src_dir": dirname(src),
                "dst_dir": normalize_path(target_dir),
                "names": [basename(src)],
                "overwrite": bool(overwrite),
                "skip_existing": bool(skip_existing),
                "merge": bool(merge),
            },
        )
        return _ok(data)

    def move_many(
        self,
        source_dir: str,
        target_dir: str,
        names: list[str],
        *,
        overwrite: bool = False,
        skip_existing: bool = True,
        merge: bool = True,
        timeout: int | float | None = None,
    ) -> bool:
        """一次请求把同一源目录下的多个文件移动到目标目录（复用 /api/fs/move 的 names 数组）。"""
        clean_names = [str(name or "").strip() for name in names if str(name or "").strip()]
        if not clean_names:
            return True
        data = self._request(
            "POST",
            "/api/fs/move",
            json={
                "src_dir": normalize_path(source_dir),
                "dst_dir": normalize_path(target_dir),
                "names": clean_names,
                "overwrite": bool(overwrite),
                "skip_existing": bool(skip_existing),
                "merge": bool(merge),
            },
            timeout=self.batch_timeout if timeout is None else timeout,
            retryable=False,
        )
        return _ok(data)

    def recursive_move(
        self,
        source_dir: str,
        target_dir: str,
        conflict_policy: str = "cancel",
        timeout: int | float | None = None,
    ) -> bool:
        """将源目录树中的文件聚合移动到目标目录。"""

        policy = str(conflict_policy or "cancel").strip().casefold()
        if policy not in {"cancel", "skip", "overwrite"}:
            raise OpenListError(f"不支持的聚合移动冲突策略：{conflict_policy}")
        data = self._bulk_request(
            "/api/fs/recursive_move",
            json={
                "src_dir": normalize_path(source_dir),
                "dst_dir": normalize_path(target_dir),
                "conflict_policy": policy,
            },
            timeout=timeout,
        )
        return _ok(data)

    def remove_empty_directory(self, path: str) -> bool:
        # remove_empty_directory 在部分云盘存储上会递归探测整棵目录树，常见表现是
        # rename/move 已成功但清空目录删除超时。Organizer 调用前会先确认目录为空，
        # 这里改用普通 remove 删除这个空目录本身，避免递归清理接口卡住。
        normalized = normalize_path(path)
        if normalized == "/":
            raise OpenListError("不能删除 OpenList 根目录")
        if self.list_dir(normalized, refresh=True):
            raise OpenListError("目录非空，拒绝删除")
        data = self._request("POST", "/api/fs/remove", json={"dir": dirname(normalized), "names": [basename(normalized)]})
        return _ok(data)

    def remove_file(self, path: str) -> bool:
        normalized = normalize_path(path)
        if normalized == "/":
            raise OpenListError("不能删除 OpenList 根目录")
        data = self._request("POST", "/api/fs/remove", json={"dir": dirname(normalized), "names": [basename(normalized)]})
        return _ok(data)

    def remove_path(self, path: str) -> bool:
        normalized = normalize_path(path)
        if normalized == "/":
            raise OpenListError("不能删除 OpenList 根目录")
        data = self._request("POST", "/api/fs/remove", json={"dir": dirname(normalized), "names": [basename(normalized)]})
        return _ok(data)

    def refresh_strm(self, path: str, *, endpoint: str = "/api/admin/scan/start", name: str = "") -> dict[str, Any]:
        api_path = str(endpoint or "").strip()
        if not api_path:
            return {"success": True, "skipped": True, "message": "OpenList STRM 同步接口未配置"}
        if not api_path.startswith("/"):
            api_path = f"/{api_path}"
        target_path = normalize_path(path)
        payload = {"path": target_path, "limit": 0}
        try:
            data = self._request("POST", api_path, json=payload)
        except OpenListError as exc:
            # 旧代码曾误用 /api/admin/scan/star。OpenList 对未知前端路由会返回
            # 200 + text/html 的 SPA 首页，不是 JSON；这里兼容旧配置/旧任务。
            if api_path.rstrip("/") != "/api/admin/scan/star" or "不是 JSON" not in str(exc):
                raise
            api_path = "/api/admin/scan/start"
            data = self._request("POST", api_path, json=payload)
        return {"success": True, "endpoint": api_path, "path": target_path, "payload": payload, "raw": data}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.base_url:
            raise OpenListError("OpenList 地址为空")
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers["Authorization"] = self.token.replace("Bearer ", "").replace("bearer ", "").strip()
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        timeout = kwargs.pop("timeout", self.timeout)
        retryable = bool(kwargs.pop("retryable", _request_is_safe_to_retry(method, path)))
        max_attempts = 1 + (self.request_retries if retryable else 0)
        last_error: requests.exceptions.RequestException | None = None
        attempts_made = 0
        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=timeout,
                    verify=self.verify_tls,
                    **kwargs,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt >= max_attempts or not _is_transient_request_error(exc):
                    break
                self._sleep_before_retry(attempt)
                continue
            data: Any = None
            json_error: ValueError | None = None
            try:
                data = response.json()
            except ValueError as exc:
                json_error = exc

            if response.status_code >= 400:
                detail = _api_error_message(data) if isinstance(data, dict) else ""
                detail = detail or " ".join((response.text or "").split())[:300]
                transient = _is_transient_http_status(response.status_code) or _is_transient_api_message(detail)
                if retryable and transient and attempt < max_attempts:
                    response.close()
                    self._sleep_before_retry(attempt)
                    continue
                retry_text = _retry_text(attempt)
                message = f"OpenList HTTP {response.status_code}{retry_text}: {detail}"
                if _is_optional_bulk_endpoint(path) and response.status_code in {404, 405}:
                    raise OpenListEndpointUnsupported(message)
                if transient:
                    raise OpenListTransientError(message)
                raise OpenListError(message)

            if json_error is not None:
                content_type = response.headers.get("content-type") or ""
                snippet = " ".join((response.text or "").split())[:180]
                if _is_optional_bulk_endpoint(path) and _looks_like_spa_html(content_type, snippet):
                    raise OpenListEndpointUnsupported(
                        f"OpenList 当前版本不支持端点 {path}：HTTP {response.status_code} {content_type}"
                    ) from json_error
                raise OpenListError(f"OpenList 返回不是 JSON：HTTP {response.status_code} {content_type} {snippet}") from json_error

            if isinstance(data, dict) and not _ok(data):
                detail = _api_error_message(data)
                transient = _is_transient_api_failure(data)
                if retryable and transient and attempt < max_attempts:
                    response.close()
                    self._sleep_before_retry(attempt)
                    continue
                message = f"OpenList 返回失败{_retry_text(attempt)}：{detail}"
                if _is_optional_bulk_endpoint(path) and _is_unsupported_api_failure(data, detail):
                    raise OpenListEndpointUnsupported(message)
                if transient:
                    raise OpenListTransientError(message)
                raise OpenListError(detail)
            return data if isinstance(data, dict) else {"data": data}

        detail = _request_error_detail(last_error)
        message = f"OpenList 请求失败：{method.upper()} {path}{_retry_text(attempts_made)}；{detail}"
        if last_error is not None and _is_transient_request_error(last_error):
            raise OpenListTransientError(message) from last_error
        raise OpenListError(message) from last_error

    def _bulk_request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        normalized_path = "/" + str(path or "").strip().lstrip("/")
        if normalized_path in self._unsupported_endpoints:
            raise OpenListEndpointUnsupported(f"OpenList 当前版本不支持端点 {normalized_path}")
        timeout = kwargs.pop("timeout", None)
        try:
            return self._request(
                "POST",
                normalized_path,
                timeout=self.batch_timeout if timeout is None else timeout,
                retryable=False,
                **kwargs,
            )
        except OpenListEndpointUnsupported:
            self._unsupported_endpoints.add(normalized_path)
            raise

    def _sleep_before_retry(self, failed_attempt: int) -> None:
        delay = self.retry_backoff_seconds * (2 ** max(0, failed_attempt - 1))
        if delay > 0:
            time.sleep(min(delay, 10.0))


def _ok(data: dict[str, Any]) -> bool:
    if isinstance(data, dict) and data.get("success") is False:
        return False
    code = data.get("code") if isinstance(data, dict) else None
    return code in (None, 0, 200, "0", "200") or data.get("success") is True


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _scoped_expected_video_paths(
    root: str,
    values: list[str] | tuple[str, ...] | set[str] | None,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    normalized_root = normalize_path(root)
    root_folded = normalized_root.casefold().rstrip("/") or "/"
    for value in values or []:
        text = str(value or "").strip()
        if not text or "://" in text:
            continue
        try:
            path = normalize_path(text)
        except OpenListError:
            continue
        path_folded = path.casefold()
        if root_folded != "/" and path_folded != root_folded and not path_folded.startswith(f"{root_folded}/"):
            continue
        if posixpath.splitext(path)[1].lower() not in VIDEO_EXTENSIONS:
            continue
        if path_folded in seen:
            continue
        seen.add(path_folded)
        result.append(path)
    return result


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _request_is_safe_to_retry(method: str, path: str) -> bool:
    normalized_method = str(method or "").strip().upper()
    normalized_path = "/" + str(path or "").strip().lstrip("/")
    return normalized_method in {"GET", "HEAD", "OPTIONS"} or normalized_path == "/api/fs/list"


def _is_optional_bulk_endpoint(path: Any) -> bool:
    normalized = "/" + str(path or "").strip().lstrip("/")
    return normalized in {
        "/api/fs/batch_rename",
        "/api/fs/regex_rename",
        "/api/fs/recursive_move",
    }


def _looks_like_spa_html(content_type: Any, snippet: Any) -> bool:
    normalized_type = str(content_type or "").casefold()
    normalized_snippet = str(snippet or "").strip().casefold()
    return "text/html" in normalized_type or normalized_snippet.startswith(("<!doctype html", "<html"))


def _is_unsupported_api_failure(data: dict[str, Any], detail: Any) -> bool:
    try:
        code = int(str(data.get("code") or data.get("status") or 0).strip())
    except (TypeError, ValueError):
        code = 0
    if code in {404, 405}:
        return True
    message = " ".join(str(detail or "").split()).casefold()
    return any(
        marker in message
        for marker in (
            "route not found",
            "endpoint not found",
            "method not allowed",
            "page not found",
            "接口不存在",
            "路由不存在",
        )
    )


def _retry_text(attempts_made: int) -> str:
    retries = max(0, int(attempts_made or 0) - 1)
    return f"，已重试 {retries} 次" if retries else ""


def _is_transient_http_status(status_code: int) -> bool:
    return int(status_code or 0) in {408, 423, 425, 429, 502, 503, 504}


def _api_error_message(data: dict[str, Any]) -> str:
    for key in ("message", "msg", "error", "detail"):
        value = data.get(key)
        if value not in (None, ""):
            return " ".join(str(value).split())[:500]
    return " ".join(str(data).split())[:500]


def _is_transient_api_failure(data: dict[str, Any]) -> bool:
    try:
        code = int(str(data.get("code") or data.get("status") or 0).strip())
    except (TypeError, ValueError):
        code = 0
    return _is_transient_http_status(code) or _is_transient_api_message(_api_error_message(data))


def _is_transient_api_message(value: Any) -> bool:
    message = " ".join(str(value or "").split()).casefold()
    markers = (
        "busy",
        "locked",
        "too many requests",
        "too many request",
        "rate limit",
        "try again later",
        "retry later",
        "please wait",
        "temporarily unavailable",
        "temporary unavailable",
        "operation in progress",
        "task is running",
        "scan is running",
        "previous task",
        "another task",
        "context deadline exceeded",
        "context canceled",
        "connection reset",
        "connection aborted",
        "remote disconnected",
        "remote end closed",
        "broken pipe",
        "request timeout",
        "read timed out",
        "connect timeout",
        "繁忙",
        "忙碌",
        "请稍后",
        "稍后重试",
        "请求过多",
        "频率过高",
        "限流",
        "被锁定",
        "已锁定",
        "任务进行中",
        "任务正在运行",
        "已有任务",
        "上一个任务",
        "临时不可用",
        "连接被对端重置",
        "对端提前关闭连接",
        "请求超时",
    )
    return any(marker in message for marker in markers)


def _is_transient_request_error(exc: requests.exceptions.RequestException) -> bool:
    detail = str(exc or "").casefold()
    if "certificate verify failed" in detail or "invalid url" in detail:
        return False
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def _request_error_detail(exc: requests.exceptions.RequestException | None) -> str:
    if exc is None:
        return "对端未返回响应"
    text = " ".join(str(exc or "").split())
    lowered = text.casefold()
    if "connection reset" in lowered or "connection aborted" in lowered:
        return "连接被对端重置"
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in lowered or "timeout" in lowered:
        return "请求超时"
    if "remote end closed" in lowered or "remote disconnected" in lowered:
        return "对端提前关闭连接"
    return text[:300] or exc.__class__.__name__
