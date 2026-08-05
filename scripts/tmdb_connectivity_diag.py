# -*- coding: utf-8 -*-
"""TMDB 连通性诊断：不吞异常，打印 DNS / TCP / HTTPS 各层真实错误。

在服务器容器内运行（把本文件复制进容器后）：
    python -X utf8 /tmp/tmdb_connectivity_diag.py
或在宿主机直接运行：
    python -X utf8 scripts/tmdb_connectivity_diag.py
"""
from __future__ import annotations

import os
import socket
import sys
import time
from urllib.parse import urlparse

HOST = "api.themoviedb.org"


def load_tmdb_config():
    import importlib.util

    candidates = [
        "/app/config/config.yaml",
        "config/config.yaml",
        "./config/config.yaml",
    ]
    config_path = next((p for p in candidates if os.path.exists(p)), None)
    if not config_path:
        print("[config] 未找到 config.yaml，跳过应用配置加载")
        return None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for root in (script_dir, os.path.dirname(script_dir), "/app", os.getcwd()):
        if root and root not in sys.path:
            sys.path.insert(0, root)
    try:
        from fnos_media_import.config import load_config
        from fnos_media_import.config_persistence import apply_persisted_config
        from fnos_media_import.database import Database
    except Exception as exc:  # noqa: BLE001
        print(f"[config] 加载应用模块失败：{exc}")
        return None
    try:
        cfg = load_config(config_path)
        db = Database(cfg.database_path)
        merged = apply_persisted_config(cfg, db.get_app_settings())
        tmdb = merged.raw.get("tmdb", {})
    except Exception as exc:  # noqa: BLE001
        print(f"[config] 读取数据库配置失败：{exc}")
        return None
    token = str(tmdb.get("token") or "").strip()
    lang = str(tmdb.get("language") or "zh-CN").strip()
    if not token:
        token = str(os.getenv("TMDB_TOKEN", "")).strip()
        print(f"[config] 数据库里 token 为空，回退用环境变量 TMDB_TOKEN：{'已设置' if token else '空'}")
    print(f"[config] 生效配置：token={'已配置' if token else '空'}  language={lang}")
    return {"token": token, "language": lang}


def env_proxies():
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]
    found = {k: v for k, v in os.environ.items() if k in keys and v.strip()}
    return found


def check_dns():
    print(f"\n[1] DNS 解析 {HOST}")
    try:
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({info[4][0] for info in infos})
        print(f"    成功，IP: {', '.join(ips)}")
        return ips
    except Exception as exc:  # noqa: BLE001
        print(f"    失败：{type(exc).__name__}: {exc}")
        return []


def check_tcp(ips):
    print("\n[2] TCP 连接 443（逐 IP 探测）")
    ok = False
    for ip in ips:
        try:
            t0 = time.time()
            sock = socket.create_connection((ip, 443), timeout=8)
            sock.close()
            print(f"    {ip}: 可连（{round(time.time()-t0, 2)}s）")
            ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"    {ip}: 失败 {type(exc).__name__}: {str(exc)[:100]}")
    return ok


def check_https(token):
    import requests

    print("\n[3] HTTPS 请求 /3/authentication（真实错误，不吞异常）")
    if not token:
        print("    跳过：token 为空，先确认服务器数据库里的 TMDB Token 是否已配置")
        return
    for label, extra in (("直连(默认环境)", {}), ):
        t0 = time.time()
        try:
            r = requests.get(
                f"https://{HOST}/3/authentication",
                params={"language": "zh-CN"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=15,
                **extra,
            )
            print(f"    [{label}] status={r.status_code} ({round(time.time()-t0, 2)}s) body={r.text[:120]}")
        except requests.exceptions.ConnectTimeout:
            print(f"    [{label}] 连接超时 → 出网 443 被丢包/屏蔽（GFW 典型表现）")
        except requests.exceptions.ReadTimeout:
            print(f"    [{label}] 读超时 → 能连上但响应被掐断（TLS 握手后丢包）")
        except requests.exceptions.ProxyError as exc:
            print(f"    [{label}] 代理错误 {str(exc)[:120]} → 环境变量代理不可用")
        except requests.exceptions.ConnectionError as exc:
            print(f"    [{label}] ConnectionError: {str(exc)[:160]}")
        except Exception as exc:  # noqa: BLE001
            print(f"    [{label}] {type(exc).__name__}: {str(exc)[:160]}")


def check_proxy_probe(proxies):
    import requests

    if not proxies:
        print("\n[4] 容器内无 HTTP(S)_PROXY 环境变量 → 直连出网，若直连被墙必然失败")
        return
    print(f"\n[4] 检测到代理环境变量：{proxies}")
    for name, value in proxies.items():
        parsed = urlparse(value)
        try:
            r = requests.get(f"https://{HOST}/3/authentication", timeout=10, proxies={"http": value, "https": value})
            print(f"    通过代理 {name}={value} → status={r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"    代理 {name}={value} 不可用：{type(exc).__name__}: {str(exc)[:100]}")


def main():
    print("==== TMDB 连通性诊断 ====")
    tmdb = load_tmdb_config()
    token = tmdb["token"] if tmdb else ""
    proxies = env_proxies()
    if proxies:
        print(f"[env] 代理环境变量：{proxies}")
    else:
        print("[env] 代理环境变量：无")
    ips = check_dns()
    tcp_ok = check_tcp(ips)
    if tcp_ok:
        check_https(token)
    else:
        print("\n[3] TCP 都无法连通 → HTTPS 必然失败，网络层问题坐实（代理或防火墙）")
    check_proxy_probe(proxies)
    print("\n==== 结论指引 ====")
    print("- DNS/TCP/HTTPS 全失败 → 服务器出网到 api.themoviedb.org 被屏蔽，需给容器配代理")
    print("- HTTPS 读超时 / ConnectionError(SSL) → 被 GFW 干扰，同样需要代理")
    print("- status=200 → 网络通，问题在应用内部调用路径，不是网络")


if __name__ == "__main__":
    main()
