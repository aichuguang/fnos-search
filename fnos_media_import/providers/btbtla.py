from __future__ import annotations

import html
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

from ..classifiers.link_classifier import detect_link


BTBTLA_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
BTBTLA_IP_CHECK_URLS = (
    "https://api64.ipify.org?format=json",
    "https://api.ipify.org?format=json",
)
PROXY_DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


logger = logging.getLogger(__name__)


@dataclass
class BtbtlaResource:
    id: str
    title: str
    url: str
    tab: str = ""
    quality: str = ""
    size_text: str = ""
    download_count: int = 0
    score: int = 0
    reasons: list[str] | None = None


class BtbtlaClient:
    def __init__(self, config: dict[str, Any], routes: dict[str, Any]):
        self.base_url = str(config.get("base_url") or "https://www.btbtla.com").strip().rstrip("/")
        self.timeout = _bounded_int(config.get("timeout"), 15, 1, 120)
        self.max_results = _bounded_int(config.get("max_results"), 20, 1, 100)
        self.max_detail_resources = _bounded_int(config.get("max_detail_resources"), 80, 1, 300)
        self.request_retries = _bounded_int(config.get("request_retries"), 4, 0, 5)
        self.retry_delay_seconds = _bounded_float(config.get("retry_delay_seconds"), 0.4, 0.0, 5.0)
        self.verify_tls = _as_bool(config.get("verify_tls"), True)
        self.use_env_proxy = _as_bool(config.get("use_env_proxy"), False)
        self.proxy_enabled = _as_bool(config.get("proxy_enabled"), False)
        self.proxy_url = str(config.get("proxy_url") or "").strip()
        self.routes = routes or {}
        self._session_lock = threading.RLock()
        self.session = requests.Session()
        self._configure_proxy()
        self.headers = {
            "User-Agent": str(config.get("user_agent") or BTBTLA_BROWSER_UA),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": f"{self.base_url}/",
        }
        self.source_origin = urlparse(self.base_url).netloc or "www.btbtla.com"

    def _configure_proxy(self) -> None:
        if not self.proxy_enabled:
            self.session.trust_env = self.use_env_proxy
            return
        if not self.proxy_url:
            raise ValueError("已启用 BT 独立代理，但未填写代理地址")
        parsed = urlparse(self.proxy_url)
        scheme = parsed.scheme.lower()
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("BT 代理端口必须是 1-65535 之间的数字") from exc
        if scheme not in PROXY_DEFAULT_PORTS or not parsed.hostname:
            raise ValueError("BT 代理仅支持 http、https、socks5 或 socks5h")
        if parsed_port is not None and not 1 <= parsed_port <= 65535:
            raise ValueError("BT 代理端口必须是 1-65535 之间的数字")
        self.session.trust_env = False
        self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})

    def close(self) -> None:
        with self._session_lock:
            self.session.close()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def search(self, keyword: str, *, limit: int | None = None) -> dict[str, Any]:
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("搜索关键词不能为空")
        url = f"{self.base_url}/search/{quote(keyword)}"
        started = time.perf_counter()
        html_text = self._get_text(url)
        rows = self._parse_search_results(html_text, keyword, limit=limit or self.max_results)
        return {
            "items": rows,
            "raw": {
                "url": url,
                "count": len(rows),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        }

    def probe_connection(self) -> dict[str, Any]:
        """Checks whether the configured direct/proxy path can really reach BTBTLA."""

        started = time.perf_counter()
        proxy = self._proxy_probe_summary()
        warnings: list[str] = []
        if proxy.get("scheme") == "socks5":
            warnings.append("socks5 由应用本地解析 DNS；建议改用 socks5h，由代理端解析 BTBTLA 域名。")
        if (
            proxy.get("source") in {"explicit", "environment"}
            and proxy.get("host") in {"127.0.0.1", "localhost", "::1"}
            and os.path.exists("/.dockerenv")
        ):
            warnings.append("当前运行在 Docker 容器内，127.0.0.1/localhost 指向容器自身；宿主机代理请使用 host.docker.internal。")

        if proxy.get("requested") and not proxy.get("configured"):
            return {
                "success": False,
                "message": "已要求 BT 搜索使用代理，但未找到可用的代理地址。",
                "mode": proxy.get("source") or "direct",
                "proxy_applied": False,
                "proxy": proxy,
                "target": {},
                "ip": {},
                "warnings": warnings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        if proxy.get("configured"):
            tcp = _probe_tcp_endpoint(
                str(proxy.get("host") or ""),
                int(proxy.get("port") or 0),
                timeout=min(float(self.timeout), 5.0),
            )
            proxy.update(tcp)
            if not tcp.get("tcp_reachable"):
                return {
                    "success": False,
                    "message": f"无法连接代理服务 {proxy.get('display') or ''}：{tcp.get('tcp_error') or '连接失败'}",
                    "mode": proxy.get("source") or "direct",
                    "proxy_applied": False,
                    "proxy": proxy,
                    "target": {},
                    "ip": {},
                    "warnings": warnings,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                }

        target = self._probe_target()
        target_attempts = int(target.get("attempts") or 0)
        if target.get("transport_ok") and target_attempts > 1:
            warnings.append(f"BTBTLA 本次第 {target_attempts} 次请求才成功，代理链路存在瞬时重置或丢包。")
        proxy_applied = bool(proxy.get("configured") and target.get("transport_ok"))
        ip_result: dict[str, Any] = {}
        if target.get("transport_ok"):
            tested_ip, tested_error = _probe_public_ip(
                self.session,
                timeout=min(float(self.timeout), 5.0),
                verify=self.verify_tls,
                user_agent=str(self.headers.get("User-Agent") or BTBTLA_BROWSER_UA),
            )
            ip_result["tested_path"] = tested_ip
            if tested_error:
                ip_result["tested_path_error"] = tested_error
                warnings.append(f"代理出口 IP 检测不稳定：{tested_error}")
            if proxy.get("configured"):
                direct_session = requests.Session()
                direct_session.trust_env = False
                try:
                    direct_ip, direct_error = _probe_public_ip(
                        direct_session,
                        timeout=min(float(self.timeout), 5.0),
                        verify=self.verify_tls,
                        user_agent=str(self.headers.get("User-Agent") or BTBTLA_BROWSER_UA),
                    )
                finally:
                    direct_session.close()
                ip_result["direct"] = direct_ip
                if direct_error:
                    ip_result["direct_error"] = direct_error
                ip_result["changed"] = bool(tested_ip and direct_ip and tested_ip != direct_ip) if tested_ip and direct_ip else None
                if tested_ip and direct_ip and tested_ip == direct_ip:
                    warnings.append("代理出口 IP 与直连 IP 相同，可能是代理开启了直连规则或两者使用同一出口。")

        target_ok = bool(target.get("ok"))
        if not target.get("transport_ok"):
            message = f"BTBTLA 连接失败：{target.get('error') or '未知错误'}"
        elif not target_ok:
            message = f"网络链路已连通，但 BTBTLA 返回 HTTP {target.get('status_code') or '-'}"
        elif proxy.get("configured") and target_attempts > 1:
            message = "代理链路已生效，BTBTLA 访问正常，但本次经过重试才连通。"
        elif proxy.get("configured"):
            message = "代理链路已生效，BTBTLA 访问正常。"
        else:
            message = "当前未使用代理，BTBTLA 直连正常。"
        return {
            "success": bool(target_ok and (not proxy.get("requested") or proxy_applied)),
            "message": message,
            "mode": proxy.get("source") or "direct",
            "proxy_applied": proxy_applied,
            "proxy": proxy,
            "target": target,
            "ip": ip_result,
            "warnings": warnings,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _proxy_probe_summary(self) -> dict[str, Any]:
        source = "direct"
        proxy_url = ""
        requested = bool(self.proxy_enabled or self.use_env_proxy)
        if self.proxy_enabled:
            source = "explicit"
            proxy_url = self.proxy_url
        elif self.use_env_proxy:
            source = "environment"
            proxy_url = _environment_proxy_for(self.base_url)
        if not proxy_url:
            return {
                "requested": requested,
                "configured": False,
                "source": source,
                "display": "",
                "scheme": "",
                "host": "",
                "port": None,
                "authentication": False,
                "dns_mode": "",
            }
        parsed = urlparse(proxy_url)
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port or PROXY_DEFAULT_PORTS.get(scheme)
        except ValueError:
            port = None
        host = parsed.hostname or ""
        configured = bool(scheme in PROXY_DEFAULT_PORTS and host and port)
        dns_mode = "remote" if scheme == "socks5h" else "local" if scheme == "socks5" else "proxy"
        return {
            "requested": requested,
            "configured": configured,
            "source": source,
            "display": _proxy_display(scheme, host, port),
            "scheme": scheme,
            "host": host,
            "port": port,
            "authentication": bool(parsed.username or parsed.password),
            "dns_mode": dns_mode,
        }

    def _probe_target(self) -> dict[str, Any]:
        url = f"{self.base_url}/"
        started = time.perf_counter()
        try:
            response, attempts = self._request(url, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "url": url,
                "transport_ok": False,
                "ok": False,
                "error": _friendly_request_error(exc),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        try:
            return {
                "url": url,
                "transport_ok": True,
                "ok": 200 <= int(response.status_code) < 400,
                "status_code": int(response.status_code),
                "final_url": str(response.url or url),
                "attempts": attempts,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        finally:
            _close_response(response)

    def detail_resources(self, detail_url: str, *, keyword: str = "", title: str = "") -> dict[str, Any]:
        safe_url = self._safe_site_url(detail_url, expected="/detail/")
        html_text = self._get_text(safe_url)
        meta = _parse_detail_meta(html_text)
        resources = self._parse_detail_resources(html_text, safe_url, keyword=keyword, title=title or meta.get("title") or "")
        return {
            "success": True,
            "detail_url": safe_url,
            "title": meta.get("title") or title,
            "meta": meta,
            "items": [resource.__dict__ for resource in resources],
            "recommended": resources[0].__dict__ if resources else {},
            "message": "BT download resources loaded" if resources else "No downloadable BT resources found",
        }

    def resolve_magnet(self, detail_url: str, *, resource_id: str = "", resource_url: str = "") -> dict[str, Any]:
        detail_url = self._safe_site_url(detail_url, expected="/detail/")
        tdown_url = ""
        if resource_url:
            tdown_url = self._safe_site_url(resource_url, expected="/tdown/")
        elif resource_id:
            safe_id = _safe_id(resource_id)
            if not safe_id:
                raise ValueError("下载资源 ID 无效")
            tdown_url = f"{self.base_url}/tdown/{safe_id}.html"
        else:
            detail_html = self._get_text(detail_url)
            match = re.search(r"/tdown/(\d+)\.html", detail_html, re.I)
            if match:
                tdown_url = f"{self.base_url}/tdown/{match.group(1)}.html"
        if not tdown_url:
            raise ValueError("未找到下载页")
        html_text = self._get_text(tdown_url)
        magnet = _extract_magnet(html_text)
        if not magnet:
            raise ValueError("Download page did not contain a magnet link")
        link = detect_link(magnet, self.routes)
        return {
            "success": True,
            "magnet": magnet,
            "source_url": magnet,
            "source_type": link.source_type,
            "supported": link.supported,
            "route": link.route,
            "tdown_url": tdown_url,
        }

    def _parse_search_results(self, html_text: str, keyword: str, *, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for block in _module_item_blocks(html_text):
            detail = _detail_match(block)
            if not detail:
                continue
            did = detail.group("id")
            if did in seen:
                continue
            seen.add(did)
            href = detail.group("href")
            title = _search_item_title(block, keyword)
            year = _search_item_year(block)
            cover = _image_url_from_html(block)
            url = urljoin(self.base_url, href)
            cover_url = _normalize_btbtla_asset_url(cover, self.base_url)
            rows.append(
                {
                    "title": title or keyword,
                    "keyword": keyword,
                    "url": url,
                    "source_url": url,
                    "source": "btbtla",
                    "source_hint": "btbtla",
                    "source_type": "bt_detail",
                    "supported": True,
                    "route": "btbtla_resolve",
                    "reason": "BTBTLA detail page; choose a download resource before resolving magnet",
                    "datetime": year,
                    "size_text": "",
                    "poster": cover_url,
                    "cover": cover_url,
                    "image_url": cover_url,
                    "source_origin": self.source_origin,
                    "referer": self.source_origin,
                    "raw_data": {
                        "btbtla_id": did,
                        "detail_url": url,
                        "cover": cover_url,
                        "poster": cover_url,
                        "source_origin": self.source_origin,
                    },
                }
            )
            if len(rows) >= limit:
                return rows

        for match in re.finditer(r'<a\b[^>]*href=["\'](?P<href>/detail/(?P<id>\d+)\.html)["\'][^>]*>(?P<label>.*?)</a>', html_text, re.I | re.S):
            did = match.group("id")
            if did in seen:
                continue
            seen.add(did)
            href = match.group("href")
            label = _clean_html(match.group("label"))
            context = _surrounding(html_text, match.start(), match.end(), 800)
            title = _best_title(label, context, keyword)
            year = ""
            cover = _image_url_from_html(context)
            url = urljoin(self.base_url, href)
            cover_url = _normalize_btbtla_asset_url(cover, self.base_url)
            rows.append(
                {
                    "title": title or keyword,
                    "keyword": keyword,
                    "url": url,
                    "source_url": url,
                    "source": "btbtla",
                    "source_hint": "btbtla",
                    "source_type": "bt_detail",
                    "supported": True,
                    "route": "btbtla_resolve",
                    "reason": "BTBTLA detail page; choose a download resource before resolving magnet",
                    "datetime": year,
                    "size_text": "",
                    "poster": cover_url,
                    "cover": cover_url,
                    "image_url": cover_url,
                    "source_origin": self.source_origin,
                    "referer": self.source_origin,
                    "raw_data": {
                        "btbtla_id": did,
                        "detail_url": url,
                        "cover": cover_url,
                        "poster": cover_url,
                        "source_origin": self.source_origin,
                    },
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _parse_detail_resources(self, html_text: str, detail_url: str, *, keyword: str, title: str) -> list[BtbtlaResource]:
        matches = list(re.finditer(r'<a\b[^>]*href=["\'](?P<href>/tdown/(?P<id>\d+)\.html)["\'][^>]*>(?P<label>.*?)</a>', html_text, re.I | re.S))
        resources: list[BtbtlaResource] = []
        seen: set[str] = set()
        active_tab = ""
        last_pos = 0
        for index, match in enumerate(matches):
            rid = match.group("id")
            if rid in seen:
                continue
            seen.add(rid)
            before = html_text[last_pos : match.start()]
            active_tab = _extract_nearest_tab(before) or active_tab
            last_pos = match.end()
            context = _surrounding(html_text, match.start(), match.end(), 900)
            label = _clean_html(match.group("label"))
            resource_title = _best_resource_title(label, context)
            if not resource_title:
                continue
            size_text = _extract_size_text(resource_title) or _extract_size_text(context)
            quality = _extract_quality(resource_title or active_tab or context)
            after = html_text[match.end() : min(len(html_text), match.end() + 1200)]
            boundary = re.search(r'<a\b[^>]*href=["\']/(?:t|p)down/(?!' + re.escape(rid) + r"\.html)\d+\.html", after, re.I)
            if boundary:
                after = after[: boundary.start()]
            else:
                for later in matches[index + 1 :]:
                    if later.group("id") != rid:
                        after = html_text[match.end() : later.start()]
                        break
            download_count = _extract_download_count(after)
            score, reasons = _score_resource(resource_title, keyword=keyword, title=title, quality=quality, tab=active_tab, size_text=size_text, download_count=download_count)
            resources.append(
                BtbtlaResource(
                    id=rid,
                    title=resource_title,
                    url=urljoin(self.base_url, match.group("href")),
                    tab=active_tab,
                    quality=quality,
                    size_text=size_text,
                    download_count=download_count,
                    score=score,
                    reasons=reasons,
                )
            )
            if len(resources) >= self.max_detail_resources:
                break
        resources.sort(key=lambda item: item.score, reverse=True)
        return resources

    def _get_text(self, url: str) -> str:
        response, _attempts = self._request(url)
        try:
            if response.status_code >= 400:
                raise RuntimeError(f"BTBTLA HTTP {response.status_code}")
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        finally:
            _close_response(response)

    def _request(self, url: str, *, allow_redirects: bool = True) -> tuple[requests.Response, int]:
        """Execute an idempotent BTBTLA GET and absorb short SOCKS/TLS resets."""

        max_attempts = self.request_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                with self._session_lock:
                    response = self.session.get(
                        url,
                        headers=self.headers,
                        timeout=(min(5.0, float(self.timeout)), float(self.timeout)),
                        verify=self.verify_tls,
                        allow_redirects=allow_redirects,
                    )
            except requests.RequestException as exc:
                if attempt >= max_attempts or not _is_retryable_request_error(exc):
                    raise
                self._prepare_retry(
                    attempt,
                    url,
                    _friendly_request_error(exc),
                )
                continue

            if int(response.status_code) not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
                return response, attempt

            status_code = int(response.status_code)
            _close_response(response)
            self._prepare_retry(attempt, url, f"HTTP {status_code}")

        raise RuntimeError("BTBTLA request retry loop ended unexpectedly")

    def _prepare_retry(self, attempt: int, url: str, reason: str) -> None:
        with self._session_lock:
            # Session.close() only clears connection pools; the configured proxies and
            # cookies remain available when requests opens the next connection.
            self.session.close()
        logger.warning(
            "btbtla transient request failure; retrying attempt=%d/%d target=%s reason=%s",
            attempt + 1,
            self.request_retries + 1,
            _safe_log_url(url),
            _redact_proxy_credentials(reason),
        )
        delay = self.retry_delay_seconds * attempt
        if delay > 0:
            time.sleep(delay)

    def _safe_site_url(self, value: str, *, expected: str = "") -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("缺少 BTBTLA 地址")
        url = urljoin(self.base_url + "/", text)
        if not url.startswith(self.base_url + "/"):
            raise ValueError("BTBTLA 地址不在允许站点内")
        if expected and expected not in url:
            raise ValueError("BTBTLA 地址类型不正确")
        return url


def _extract_magnet(html_text: str) -> str:
    for pattern in (
        r"magnet:\?xt=urn:btih:([a-fA-F0-9]{40})",
        r"<meta\s+name=[\"']keywords[\"'][^>]*?([a-fA-F0-9]{40})",
        r"\b([a-fA-F0-9]{40})\b",
    ):
        match = re.search(pattern, html_text, re.I)
        if match:
            return f"magnet:?xt=urn:btih:{match.group(1).lower()}"
    return ""


def _environment_proxy_for(target_url: str) -> str:
    proxies = requests.utils.get_environ_proxies(target_url)
    target_scheme = urlparse(target_url).scheme.lower()
    for key in (target_scheme, "all"):
        value = str(proxies.get(key) or "").strip()
        if value:
            return value
    return ""


def _proxy_display(scheme: str, host: str, port: int | None) -> str:
    if not scheme or not host:
        return ""
    safe_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{safe_host}{f':{port}' if port else ''}"


def _probe_tcp_endpoint(host: str, port: int, *, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    if not host or not port:
        return {"tcp_reachable": False, "tcp_error": "代理主机或端口无效", "tcp_elapsed_ms": 0.0}
    connection = None
    try:
        connection = socket.create_connection((host, port), timeout=max(0.5, timeout))
    except OSError as exc:
        return {
            "tcp_reachable": False,
            "tcp_error": _redact_proxy_credentials(str(exc) or exc.__class__.__name__),
            "tcp_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    return {
        "tcp_reachable": True,
        "tcp_error": "",
        "tcp_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _probe_public_ip(
    session: requests.Session,
    *,
    timeout: float,
    verify: bool,
    user_agent: str,
) -> tuple[str, str]:
    last_error = ""
    headers = {"User-Agent": user_agent, "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8"}
    for url in BTBTLA_IP_CHECK_URLS:
        try:
            response = session.get(url, headers=headers, timeout=max(1.0, timeout), verify=verify, allow_redirects=True)
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                continue
            try:
                payload = response.json()
            except ValueError:
                payload = None
            candidate = str(payload.get("ip") or "").strip() if isinstance(payload, dict) else str(response.text or "").strip()
            candidate = candidate.splitlines()[0].strip()[:128] if candidate else ""
            try:
                return str(ipaddress.ip_address(candidate)), ""
            except ValueError:
                last_error = "出口 IP 服务返回了无效地址"
        except Exception as exc:  # noqa: BLE001
            last_error = _friendly_request_error(exc)
    return "", last_error or "无法读取出口 IP"


def _friendly_request_error(exc: Exception) -> str:
    text = _redact_proxy_credentials(str(exc) or exc.__class__.__name__)
    lowered = text.lower()
    if "missing dependencies for socks support" in lowered:
        return "SOCKS5 支持未安装，请确认环境已安装 PySocks"
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"代理连接失败：{text}"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"连接超时：{text}"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"TLS 校验失败：{text}"
    return text[:800]


def _is_retryable_request_error(exc: requests.RequestException) -> bool:
    text = str(exc or "").lower()
    permanent_markers = (
        "authentication failed",
        "certificate verify failed",
        "failed to parse",
        "getaddrinfo failed",
        "invalid proxy url",
        "invalid url",
        "missing dependencies for socks support",
        "proxy authentication required",
    )
    if any(marker in text for marker in permanent_markers):
        return False
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ProxyError,
            requests.exceptions.ReadTimeout,
        ),
    )


def _close_response(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        closer()


def _redact_proxy_credentials(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"(?i)\b(https?|socks5h?)://[^\s/@]*@", r"\1://***@", text)


def _safe_log_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    path = parsed.path or "/"
    if not parsed.scheme or not parsed.hostname:
        return path
    safe_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{safe_host}{port}{path}"


def _module_item_blocks(html_text: str) -> list[str]:
    starts = [
        match.start()
        for match in re.finditer(
            r'<div\b[^>]*class=["\'][^"\']*(?<![\w-])module-item(?![\w-])[^"\']*["\'][^>]*>',
            html_text or "",
            re.I,
        )
    ]
    if not starts:
        return []
    starts.append(len(html_text or ""))
    return [html_text[starts[index] : starts[index + 1]] for index in range(len(starts) - 1)]


def _detail_match(block: str) -> re.Match[str] | None:
    return re.search(r'<a\b[^>]*href=["\'](?P<href>/detail/(?P<id>\d+)\.html)["\'][^>]*>(?P<label>.*?)</a>', block or "", re.I | re.S)


def _search_item_title(block: str, keyword: str) -> str:
    candidates: list[str] = []
    for pattern in (
        r'<a\b[^>]*class=["\'][^"\']*module-item-title[^"\']*["\'][^>]*>(.*?)</a>',
        r'<div\b[^>]*class=["\'][^"\']*video-name[^"\']*["\'][^>]*>.*?<a\b[^>]*>(.*?)</a>',
    ):
        candidates.append(_clean_html(_first_match(block, pattern, flags=re.I | re.S)))
    for pattern in (
        r'<a\b[^>]*class=["\'][^"\']*module-item-title[^"\']*["\'][^>]*title=["\']([^"\']+)["\']',
        r'<div\b[^>]*class=["\'][^"\']*video-name[^"\']*["\'][^>]*>.*?<a\b[^>]*title=["\']([^"\']+)["\']',
        r'<a\b[^>]*href=["\']/detail/\d+\.html["\'][^>]*title=["\']([^"\']+)["\']',
        r'<img\b[^>]*alt=["\']([^"\']+)["\']',
    ):
        candidates.append(html.unescape(_first_match(block, pattern, flags=re.I | re.S)).strip())
    detail = _detail_match(block)
    if detail:
        candidates.append(_clean_html(detail.group("label")))
    for candidate in candidates:
        text = re.sub(r"\s+", " ", str(candidate or "")).strip(" -_")
        if text and len(text) >= 2 and not re.fullmatch(r"\d+", text):
            return text
    return keyword


def _search_item_year(block: str) -> str:
    text = _clean_html(block)
    for pattern in (
        r"(?:年份|年代|上映|首播|year|release)\D{0,20}((?:19|20)\d{2})",
        r"(?:module-item-year|video-year)[\s\S]{0,80}?((?:19|20)\d{2})",
    ):
        year = _first_match(text if "module-item-year" not in pattern else block, pattern, flags=re.I)
        if year:
            return year
    return ""


def _image_url_from_html(block: str) -> str:
    for pattern in (
        r'<img\b[^>]*\b(?:data-src|data-original|data-lazy-src|data-url|lay-src|original)=["\']([^"\']+)["\']',
        r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']',
        r'background(?:-image)?\s*:\s*url\(["\']?([^"\')]+)',
    ):
        value = _first_match(block, pattern, flags=re.I | re.S)
        if value:
            return _html_unescape_repeated(value)
    return ""


def _normalize_btbtla_asset_url(value: str, base_url: str) -> str:
    text = _html_unescape_repeated(value)
    if not text or text.lower().startswith("data:"):
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    parsed = urlparse(text)
    if parsed.netloc.lower().endswith("sogoucdn.com"):
        real_url = (parse_qs(parsed.query).get("url") or [""])[0]
        if real_url:
            return _normalize_btbtla_asset_url(real_url, base_url)
    if re.match(r"^(?:image\.tmdb\.org|img\d*\.doubanio\.com|p\d+\.ssl\.qhimgs\d*\.com|ps\.ssl\.qhmsg\.com|img\d+\.sogoucdn\.com)/", text, re.I):
        return f"https://{text}"
    return urljoin(base_url, text)


def _html_unescape_repeated(value: Any) -> str:
    text = str(value or "").strip()
    for _ in range(3):
        decoded = html.unescape(text).strip()
        if decoded == text:
            break
        text = decoded
    return text


def _parse_detail_meta(html_text: str) -> dict[str, Any]:
    title = _clean_html(_first_match(html_text, r"<h1[^>]*>(.*?)</h1>", flags=re.I | re.S) or _first_match(html_text, r"<title[^>]*>(.*?)</title>", flags=re.I | re.S))
    title = re.sub(r"[-_｜|].*$", "", title).strip()
    episode = _first_match(html_text, r"(?:集数|集數)\s*[：:]\s*(\d+)")
    year = _first_match(html_text, r"(?<!\d)((?:19|20)\d{2})(?!\d)")
    return {"title": title, "episode_count": int(episode) if str(episode).isdigit() else None, "year": year}


def _best_title(label: str, context: str, keyword: str) -> str:
    candidates = [label, _clean_html(_first_match(context, r"<h[1-4][^>]*>(.*?)</h[1-4]>", flags=re.I | re.S))]
    for candidate in candidates:
        text = candidate.strip()
        if text and len(text) >= 2 and not re.fullmatch(r"\d+", text):
            return text
    return keyword


def _best_resource_title(label: str, context: str) -> str:
    context_text = _clean_html(context)
    candidates = [
        label,
        _clean_html(_first_match(context, r"<h[1-5][^>]*>(.*?)</h[1-5]>", flags=re.I | re.S)),
        _clean_html(_first_match(context, r"<span[^>]*>(.*?)</span>", flags=re.I | re.S)),
        _resource_like_text(context_text),
    ]
    for candidate in candidates:
        text = candidate.strip()
        if not text or len(text) <= 2:
            continue
        if re.fullmatch(r"\d+", text):
            continue
        if "下载" in text and len(text) <= 8:
            continue
        return text
    return ""


def _resource_like_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    patterns = [
        r"([^\n\r<>]{8,220}(?:\[\s*\d+(?:\.\d+)?\s*(?:GB|MB|G|M)\s*\]|(?:\d+(?:\.\d+)?\s*(?:GB|MB|G|M))))",
        r"([^\n\r<>]{8,220}(?:1080p|2160p|720p|4k)[^\n\r<>]{0,120})",
        r"([^\n\r<>]{8,220}(?:全集|全\d{1,3}集|01\s*[-~_至－—]\s*\d{2,3}|fin|complete)[^\n\r<>]{0,120})",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, clean, re.I):
            value = match.group(1).strip(" -_｜|")
            if value:
                candidates.append(value)
    if candidates:
        candidates.sort(key=lambda item: (("[" in item or "]" in item), len(item)), reverse=True)
        return candidates[0]
    return ""


def _score_resource(resource_title: str, *, keyword: str, title: str = "", quality: str, tab: str, size_text: str, download_count: int) -> tuple[int, list[str]]:
    text = resource_title.lower()
    normalized = _normalize_text(resource_title)
    keyword_norm = _normalize_text(keyword)
    score = 0
    reasons: list[str] = []
    if keyword_norm and keyword_norm in normalized:
        score += 80
        reasons.append("标题匹配搜索词")
    if re.search(r"(?:0?1|第?1[集话話]?)\s*[-~_至－—]\s*(?:\d{2,3})", text) or re.search(r"(?:全集|全\d{1,3}集|complete|fin\b|完结|完結)", text, re.I):
        score += 120
        reasons.append("整季/全集资源")
    if re.search(r"(?:s\d{1,2}|season|第\s*\d+\s*季|\d+(?:st|nd|rd|th)\s*season)", text, re.I):
        score += 45
        reasons.append("包含季信息")
    if re.search(r"\[(?:0?\d{1,3})\]|\b(?:e|ep)\s*\d{1,3}\b", text, re.I) and not re.search(r"\d{1,3}\s*[-~_至－—]\s*\d{1,3}", text):
        score -= 45
        reasons.append("单集资源降权")
    quality_text = (quality or tab or title).lower()
    if "2160" in quality_text or "4k" in quality_text:
        score += 120
        reasons.append("2160p/4K")
    elif "1080" in quality_text:
        score += 28
        reasons.append("1080p")
    elif "720" in quality_text:
        score += 10
        reasons.append("720p")
    size_bytes = _parse_size_bytes(size_text)
    if 100 * 1024 * 1024 <= size_bytes <= 200 * 1024 * 1024 * 1024:
        score += 18
        reasons.append("大小合理")
    elif 0 < size_bytes < 100 * 1024 * 1024:
        score -= 80
        reasons.append("小文件降权")
    if download_count:
        score += min(120, download_count)
        reasons.append(f"下载 {download_count}")
    return score, reasons


def _extract_nearest_tab(html_text: str) -> str:
    tail = html_text[-1200:]
    labels = re.findall(r">(1080p|2160p|4k|other|夸克网盘|百度网盘|磁力|torrent)<", tail, re.I)
    return labels[-1] if labels else ""


def _extract_quality(value: str) -> str:
    text = str(value or "").lower()
    if "2160" in text or "4k" in text:
        return "2160p"
    if "1080" in text:
        return "1080p"
    if "720" in text:
        return "720p"
    return ""


def _extract_size_text(value: str) -> str:
    match = re.search(r"\[?\s*(\d+(?:\.\d+)?)\s*(GB|MB|G|M)\s*\]?", str(value or ""), re.I)
    if not match:
        return ""
    unit = match.group(2).upper()
    return f"{match.group(1)}{'GB' if unit == 'G' else 'MB' if unit == 'M' else unit}"


def _parse_size_bytes(value: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|G|M)", str(value or ""), re.I)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).lower()
    return int(number * (1024**3 if unit in {"g", "gb"} else 1024**2))


def _extract_download_count(value: str) -> int:
    text = str(value or "")
    patterns = [
        r'class=["\'][^"\']*(?:btn-down|download)[^"\']*["\'][^>]*>.*?<span[^>]*>\s*(\d{1,6})\s*</span>',
        r'title=["\']下载量[^"\']*["\'][^>]*>.*?<span[^>]*>\s*(\d{1,6})\s*</span>',
        r'(?:icon-download|fa-download)[\s\S]{0,160}?<span[^>]*>\s*(\d{1,6})\s*</span>',
        r'(?:下载量|下载次数|downloads?)\D{0,80}(\d{1,6})',
    ]
    for pattern in patterns:
        numbers = [int(item) for item in re.findall(pattern, text, re.I | re.S)]
        if numbers:
            return max(numbers)
    return 0


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _surrounding(text: str, start: int, end: int, size: int) -> str:
    return text[max(0, start - size) : min(len(text), end + size)]


def _first_match(text: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, text or "", flags)
    return match.group(1).strip() if match else ""


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _safe_id(value: str) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{1,20}", text) else ""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}
