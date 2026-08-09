"""OpenList WebDAV 客户端（urllib 实现，仅 http/https，不读环境代理）。

同时供 ``OpenListConnectionService`` 的认证探测与 ``OpenListMountService`` 的
目录选择/读写检测使用。测试通过注入 ``webdav_factory`` 模拟，不依赖真实网络。
"""

from __future__ import annotations

import base64
import posixpath
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from . import OpenListValidationError, OpenListWebDAVError
from .locator_resolver import normalize_openlist_path

_DAV = "{DAV:}"
_AUTHENTICATED_STATUSES = frozenset({200, 201, 204, 207, 405})


class OpenListWebDAVClient:
    """最小 WebDAV 客户端：PROPFIND / MKCOL / PUT / GET / MOVE / DELETE。"""

    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: int = 15,
    ) -> None:
        self.base_url, self.username, self.password, self.timeout = self._validate(
            base_url, username, password, timeout
        )

    @staticmethod
    def _validate(base_url: str, username: str, password: str, timeout: int) -> tuple[str, str, str, int]:
        text = str(base_url or "").strip()
        if not text:
            raise OpenListValidationError("OpenList base_url 为空")
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in ("http", "https"):
            raise OpenListValidationError("OpenList base_url 只允许 http/https")
        if parsed.username or parsed.password:
            raise OpenListValidationError("OpenList base_url 不允许内嵌用户名/密码")
        if parsed.query or parsed.fragment:
            raise OpenListValidationError("OpenList base_url 不允许包含查询参数或锚点")
        if not parsed.hostname:
            raise OpenListValidationError("OpenList base_url 缺少主机名")
        if " " in text:
            raise OpenListValidationError("OpenList base_url 不允许包含空白字符")
        try:
            timeout = max(1, min(120, int(timeout or 15)))
        except (TypeError, ValueError):
            timeout = 15
        return text.rstrip("/"), str(username or ""), str(password or ""), timeout

    def _build_url(self, path: str) -> str:
        """拼接 base_url 与 WebDAV 路径，并对非 ASCII 做百分号编码。

        urllib 不自动编码路径里的中文，直接放进 Request 会在 urlopen 时抛
        ascii 编码错误；这里保留 / 与已编码的 %，仅转义中文等字符。
        """
        from urllib.parse import quote

        return self.base_url + quote(normalize_openlist_path(path), safe="/%:@?&=+#")

    def _request(self, method: str, path: str, *, headers: dict[str, Any] | None = None, body: bytes | None = None) -> tuple[int, bytes]:
        url = self._build_url(path)
        request = urllib.request.Request(url, method=method, data=body, headers=dict(headers or {}))
        request.add_header("User-Agent", "fnos-openlist/1.0")
        if self.username or self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        # OpenList 常部署在局域网 / Docker 网关；默认不读取环境代理，避免误走
        # 本机 HTTP_PROXY 导致内网请求超时。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise OpenListWebDAVError(f"WebDAV 请求失败：{exc}")

    def auth_probe(self) -> None:
        """认证与服务版本探测：PROPFIND 根（Depth 0）。401 即认证失败。"""
        status, _ = self._request("PROPFIND", "/", headers={"Depth": "0"})
        if status == 401:
            raise OpenListWebDAVError("WebDAV 认证失败（HTTP 401）")
        if status in _AUTHENTICATED_STATUSES:
            return
        raise OpenListWebDAVError(f"WebDAV 探测失败：HTTP {status}")

    def list_dir(self, path: str, *, depth: str = "1") -> list[dict[str, Any]]:
        status, body = self._request("PROPFIND", path, headers={"Depth": depth})
        if status not in (200, 207):
            if status == 404:
                raise OpenListWebDAVError(f"目录不存在：{normalize_openlist_path(path)}")
            raise OpenListWebDAVError(f"列出目录失败：HTTP {status}")
        return _parse_multistatus(body, base_url=self.base_url)

    def mkcol(self, path: str) -> None:
        status, _ = self._request("MKCOL", path)
        if status not in (200, 201, 204, 405):
            raise OpenListWebDAVError(f"创建目录失败：HTTP {status}")

    def put_bytes(self, path: str, content: bytes) -> None:
        # urllib 默认把 data= 当表单提交（application/x-www-form-urlencoded），
        # 部分 WebDAV 服务器对非文件上传的 Content-Type 返回 405；显式声明文件类型。
        status, _ = self._request(
            "PUT",
            path,
            body=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        if status not in (200, 201, 204):
            raise OpenListWebDAVError(f"写入文件失败：HTTP {status}")

    def read_bytes(self, path: str) -> bytes:
        status, body = self._request("GET", path)
        if status not in (200, 206):
            raise OpenListWebDAVError(f"读取文件失败：HTTP {status}")
        return body

    def file_size(self, path: str) -> int | None:
        for item in self.list_dir(path, depth="0"):
            if item.get("size") is not None:
                return int(item["size"])
        return None

    def move(self, source: str, destination: str) -> None:
        headers = {"Destination": self._build_url(destination)}
        status, _ = self._request("MOVE", source, headers=headers)
        if status not in (200, 201, 204):
            raise OpenListWebDAVError(f"移动文件失败：HTTP {status}")

    def delete(self, path: str) -> None:
        status, _ = self._request("DELETE", path)
        if status not in (200, 202, 204, 404):
            raise OpenListWebDAVError(f"删除失败：HTTP {status}")

    def exists(self, path: str) -> bool:
        status, _ = self._request("PROPFIND", path, headers={"Depth": "0"})
        return status in (200, 207)


def _parse_multistatus(body: bytes, *, base_url: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise OpenListWebDAVError(f"WebDAV 响应解析失败：{exc}") from exc
    items: list[dict[str, Any]] = []
    for response in root.findall(f"{_DAV}response"):
        href_element = response.find(f"{_DAV}href")
        if href_element is None:
            continue
        href = urllib.parse.unquote((href_element.text or "").strip())
        if not href:
            continue
        path = _href_to_path(href, base_url)
        if path is None:
            continue
        is_dir = False
        size: int | None = None
        for propstat in response.findall(f"{_DAV}propstat"):
            for prop in propstat.findall(f"{_DAV}prop"):
                resource_type = prop.find(f"{_DAV}resourcetype")
                if resource_type is not None and resource_type.find(f"{_DAV}collection") is not None:
                    is_dir = True
                content_length = prop.find(f"{_DAV}getcontentlength")
                if content_length is not None and content_length.text:
                    try:
                        size = int(content_length.text)
                    except ValueError:
                        pass
        name = posixpath.basename(path.rstrip("/")) or "/"
        items.append({"path": path, "name": name, "is_dir": is_dir, "size": size})
    return items


def _href_to_path(href: str, base_url: str) -> str | None:
    if href.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(href)
        base = urllib.parse.urlparse(base_url)
        if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
            return None
        path = parsed.path
    else:
        path = href
    base_path = urllib.parse.urlparse(base_url).path.rstrip("/")
    if base_path:
        if path == base_path:
            path = "/"
        elif path.startswith(base_path + "/"):
            path = path[len(base_path):]
    return normalize_openlist_path(path)


__all__ = ["OpenListWebDAVClient"]
