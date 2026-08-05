from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from flask import session

from .classifiers.link_classifier import detect_link
from .constants import CATEGORY_LABELS
from .importers.cloud139 import Cloud139Importer
from .importers.generic import GenericWebhookImporter
from .importers.sixpan import SixPanOfflineImporter
from .services.public_resource_detail_service import PublicResourceDetailService
from .services.public_submission_preflight_service import PublicSubmissionPreflightService
from .web_input import _clip_text, _config_bool, _config_int, _safe_int_value


def _hash_password(password: str) -> str:
    iterations = 180_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_password_hash(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_text, salt, digest = str(stored_hash or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        candidate = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), iterations).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def _safe_public_string_list(value: Any, max_items: int = 200, max_length: int = 512) -> list[str]:
    if value is None or value == "":
        return []
    raw = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw[: max(1, max_items)]:
        text = str(item or "").strip()
        if text and len(text) <= max_length and text not in result:
            result.append(text)
    return result


def _safe_public_quark_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    mode = str(value.get("mode") or "").strip()
    if mode not in {"root_dirs", "subdir_items"}:
        return {}

    def _entry(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        fid = _clip_text(raw.get("fid"), 256)
        name = _clip_text(raw.get("name"), 240)
        if not fid:
            return None
        type_text = str(raw.get("type") or raw.get("kind") or "").strip().lower()
        is_dir = bool(raw.get("is_dir") or raw.get("dir") or type_text in {"dir", "folder", "directory"})
        return {
            "fid": fid,
            "name": name or fid,
            "type": "dir" if is_dir else "file",
            "is_dir": is_dir,
        }

    def _entry_list(raw: Any, max_items: int = 100) -> list[dict[str, Any]]:
        rows = raw if isinstance(raw, list) else []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows[:max_items]:
            entry = _entry(item)
            if not entry or entry["fid"] in seen:
                continue
            seen.add(entry["fid"])
            result.append(entry)
        return result

    if mode == "root_dirs":
        selected_dirs = [item for item in _entry_list(value.get("selected_dirs"), max_items=100) if item.get("is_dir")]
        return {"mode": "root_dirs", "selected_dirs": selected_dirs} if selected_dirs else {}

    base_dir = _entry(value.get("base_dir"))
    if not base_dir or not base_dir.get("is_dir"):
        return {}
    selected_items = _entry_list(value.get("selected_items"), max_items=200)
    return {"mode": "subdir_items", "base_dir": base_dir, "selected_items": selected_items} if selected_items else {"mode": "subdir_items", "base_dir": base_dir, "selected_items": []}


def _safe_public_cloud139_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    mode = str(value.get("mode") or "").strip()
    if mode not in {"items", "folders"}:
        return {}

    def _item_rows(raw_rows: Any, *, is_dir: bool, max_items: int) -> list[dict[str, Any]]:
        rows = raw_rows if isinstance(raw_rows, list) else []
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows[:max_items]:
            if not isinstance(raw, dict):
                continue
            fid = _clip_text(raw.get("fid") or raw.get("id"), 256)
            name = _clip_text(raw.get("name") or raw.get("fileName"), 240) or fid
            path = _clip_text(raw.get("path"), 1024)
            token = _clip_text(raw.get("share_fid_token"), 2048)
            key = path or token or fid
            if not key or key in seen:
                continue
            seen.add(key)
            selected.append(
                {
                    "fid": fid,
                    "name": name or key,
                    "type": "dir" if is_dir else "file",
                    "is_dir": is_dir,
                    "path": path,
                    "share_fid_token": token,
                }
            )
        return selected

    selected_files = _item_rows(value.get("selected_files"), is_dir=False, max_items=300)
    selected_folders = _item_rows(value.get("selected_folders"), is_dir=True, max_items=100)
    if not selected_files and not selected_folders:
        return {}
    return {"mode": "items", "selected_files": selected_files, "selected_folders": selected_folders}


def _safe_public_sixpan_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    result: dict[str, Any] = {}
    for key in ("total_count", "selected_count", "ignored_count"):
        if key not in value or isinstance(value.get(key), bool):
            continue
        try:
            count = int(value.get(key))
        except (TypeError, ValueError):
            continue
        if count >= 0:
            result[key] = min(count, 100_000)

    if "total_count" in result and "selected_count" in result:
        result["selected_count"] = min(result["selected_count"], result["total_count"])

    parse_status = str(value.get("parse_status") or "").strip().lower()
    if parse_status in {"files_ready", "empty_files", "parse_failed"}:
        result["parse_status"] = parse_status
    parse_error = _clip_text(value.get("parse_error"), 500)
    if parse_error:
        result["parse_error"] = parse_error
    if isinstance(value.get("slow"), bool):
        result["slow"] = value["slow"]
    if "ignore_files" in value:
        ignore_files = _safe_public_string_list(value.get("ignore_files"), max_items=2000, max_length=512)
        result["ignore_files"] = ignore_files
        result["ignored_count"] = len(ignore_files)
    return result


def _public_security_config(security_config: dict[str, Any]) -> dict[str, Any]:
    captcha_enabled = _config_bool(security_config, "captcha_enabled", False)
    return {
        "captcha": {
            "enabled": captcha_enabled,
            "provider": "simple" if captcha_enabled else "none",
        },
        "limits": {
            "max_keyword_length": _config_int(security_config, "max_keyword_length", 80),
            "max_title_length": _config_int(security_config, "max_title_length", 300),
            "max_url_length": _config_int(security_config, "max_url_length", 2048),
            "max_note_length": _config_int(security_config, "max_note_length", 500),
        },
    }


def _adapter_placeholders(
    config: dict[str, Any],
    generic_importers: dict[str, Any],
    cloud139_importer: Cloud139Importer | None = None,
) -> list[dict[str, Any]]:
    routes = config.get("routes", {})
    quark_config = config.get("quark", {})

    items = [
        {
            "key": "quark",
            "name": "Quark 自动转存",
            "source_types": ["quark", "uc"],
            "route": "quark_to_mobile",
            "adapter_type": "quark_auto_save",
            "enabled": bool(routes.get("quark", {}).get("enabled", True)),
            "configured": bool(quark_config.get("auto_save_url") and quark_config.get("token")),
            "status": "implemented",
            "message": "当前主链路已接入",
            "capabilities": {"submit": True, "task_poll": False, "direct_refresh": False},
        }
    ]

    if cloud139_importer:
        description = cloud139_importer.describe()
        route_enabled = _config_bool(routes.get("cloud139", {}), "enabled", False)
        items.append(
            {
                "key": "cloud139",
                "name": "139 移动云官方直转",
                "source_types": ["cloud139"],
                "route": "cloud139_direct",
                "adapter_type": "cloud139_cmcc_native",
                "enabled": route_enabled,
                "configured": bool(description.get("configured")),
                "status": "implemented" if description.get("configured") else "unconfigured",
                "message": "已接入 139 移动云官方直转，复用移动云官方上传 CMCC 认证" if description.get("configured") else "139 官方直转未配置，请在“移动云官方上传”中维护 CMCC Basic 认证",
                "capabilities": description.get("capabilities", {}),
                "config": description.get("config", {}),
            }
        )

    placeholder_specs = [
        ("cloud189", "天翼云", ["cloud189"], "cloud189_direct", "后续接天翼云专用适配器；当前仅保留通用 Webhook 入口"),
        ("sixpan", "磁链 / 种子离线", ["magnet", "torrent"], "sixpan_offline", "后续接 6盘或其他离线下载适配器；当前仅保留通用 Webhook 入口"),
    ]
    for key, name, source_types, route, message in placeholder_specs:
        route_enabled = any(_config_bool(routes.get(source_type, {}), "enabled", False) for source_type in source_types)
        importer = generic_importers.get(key)
        description = importer.describe() if importer and hasattr(importer, "describe") else {}
        adapter_type = str(description.get("adapter_type") or "placeholder_webhook")
        configured = bool(description.get("configured"))
        implemented = adapter_type == "sixpan_offline" or (
            adapter_type == "generic_webhook" and configured
        )
        if adapter_type == "sixpan_offline":
            item_message = "已接入六盘 OpenAPI 离线任务适配器"
        elif adapter_type == "generic_webhook" and configured:
            item_message = "已配置天翼云通用 Webhook；当前尚未接入专用平台 API"
        else:
            item_message = message
        items.append(
            {
                "key": key,
                "name": name,
                "source_types": source_types,
                "route": route,
                "adapter_type": adapter_type,
                "enabled": route_enabled,
                "configured": configured,
                "status": "implemented" if implemented and configured else ("unconfigured" if importer else "placeholder"),
                "message": item_message,
                "capabilities": {
                    "submit": bool(description.get("capabilities", {}).get("submit")),
                    "task_poll": bool(description.get("capabilities", {}).get("task_poll")),
                    "direct_refresh": bool(description.get("capabilities", {}).get("refresh_after_submit")),
                },
                "config": description.get("config", {}),
            }
        )
    return items


def _public_adapter_capabilities(config: dict[str, Any]) -> list[dict[str, Any]]:
    public_names = {
        "cloud139": "快速入库",
        "quark": "网盘自动入库",
        "sixpan": "云端处理",
        "cloud189": "天翼云入库",
    }
    public_items = []
    cloud139_importer = Cloud139Importer(config.get("cloud139", {}), cmcc_config=config.get("cmcc_upload", {}))
    sixpan_importer = SixPanOfflineImporter(config.get("sixpan", {}))
    cloud189_importer = GenericWebhookImporter("天翼云", config.get("cloud189", {}))
    for item in _adapter_placeholders(
        config,
        {"sixpan": sixpan_importer, "cloud189": cloud189_importer},
        cloud139_importer=cloud139_importer,
    ):
        capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        usable = bool(item.get("enabled") and item.get("configured") and capabilities.get("submit"))
        public_items.append(
            {
                "key": item["key"],
                "name": public_names.get(str(item["key"] or ""), item["name"]),
                "source_types": item["source_types"],
                "enabled": usable,
                "route_enabled": bool(item.get("enabled")),
                "configured": bool(item.get("configured")),
                "status": item["status"] if usable else "unavailable",
                "capabilities": capabilities,
            }
        )
    return public_items


def _new_simple_captcha(security_config: dict[str, Any], secret_key: str) -> dict[str, Any]:
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    answer = str(left + right)
    ttl = max(60, _config_int(security_config, "captcha_ttl_seconds", 300))
    return {
        "question": f"{left} + {right} = ?",
        "answer_hash": _captcha_hash(answer, secret_key),
        "expires_in_seconds": ttl,
    }


def _captcha_hash(answer: str, secret_key: str) -> str:
    return hmac.new(str(secret_key or "").encode("utf-8"), str(answer or "").strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_public_captcha(payload: dict[str, Any], security_config: dict[str, Any], client_ip: str, secret_key: str) -> tuple[bool, str]:
    del client_ip
    if not _config_bool(security_config, "captcha_enabled", False):
        return True, ""
    answer = str(payload.get("captcha_answer") or "").strip()
    expected = str(session.get("public_captcha_hash") or "")
    expires_at = int(session.get("public_captcha_expires_at") or 0)
    if not answer or not expected:
        return False, "请先完成验证码"
    if expires_at < int(time.time()):
        session.pop("public_captcha_hash", None)
        session.pop("public_captcha_expires_at", None)
        return False, "验证码已过期，请刷新后重试"
    if not hmac.compare_digest(_captcha_hash(answer, secret_key), expected):
        return False, "验证码错误"
    session.pop("public_captcha_hash", None)
    session.pop("public_captcha_expires_at", None)
    return True, ""


def _public_routes(routes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "enabled": _config_bool(value, "enabled", False),
        }
        for key, value in routes.items()
    }


def _public_resource_item(
    item: dict[str, Any],
    public_id: str,
    hide_full_links: bool = True,
) -> dict[str, Any]:
    source_url = str(item.get("url") or item.get("source_url") or "")
    raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    poster = str(item.get("poster") or item.get("cover") or item.get("image_url") or raw.get("poster") or raw.get("cover") or raw.get("image_url") or "").strip()
    source_origin = str(item.get("source_origin") or raw.get("source_origin") or "").strip()
    instant_import = _is_cloud139_public_item(item)
    result = {
        "public_id": public_id,
        "title": item.get("title") or "未命名资源",
        "supported": bool(item.get("supported", False)),
        "reason": "已识别资源类型，提交时会检测可用性" if item.get("supported") else "当前资源暂不支持自动入库",
        "source_type": item.get("source_type") or item.get("source") or "",
        "source_hint": item.get("source_hint") or item.get("source") or item.get("provider") or "",
        "route": item.get("route") or "",
        "availability_status": item.get("availability_status") or "",
        "availability_message": item.get("availability_message") or "",
        "datetime": item.get("datetime") or item.get("created_at") or "",
        "size": item.get("size"),
        "size_text": item.get("size_text") or item.get("size") or "",
        "quality_tags": item.get("quality_tags") or [],
        "duplicate_count": item.get("duplicate_count") or 1,
        "rank": int(item.get("rank") or 0),
        "relevance_score": int(item.get("relevance_score") or 0),
        "ranking_score": int(item.get("ranking_score") or 0),
        "category_suggestion": _category_suggestion(item),
        "result_key": _public_result_key(item),
        "instant_import": instant_import,
        "speed_tag": "快速入库" if instant_import else "",
    }
    if poster:
        result["poster"] = poster
        result["cover"] = poster
        result["image_url"] = poster
    if source_origin:
        result["source_origin"] = source_origin
        result["referer"] = source_origin
    if source_url:
        if hide_full_links:
            result["source_url_masked"] = _mask_share_url(source_url)
        else:
            result["source_url"] = source_url
    return result


def _public_result_key(item: dict[str, Any]) -> str:
    source_url = str(item.get("url") or item.get("source_url") or "").strip()
    source_type = str(item.get("source_type") or item.get("source") or "").strip().lower()
    title = str(item.get("title") or item.get("name") or item.get("note") or "").strip()
    raw_key = f"{source_type}|{source_url or title}"
    return hashlib.sha256(raw_key.encode("utf-8", "ignore")).hexdigest()[:20]


def _is_cloud139_public_item(item: dict[str, Any]) -> bool:
    source_type = str(item.get("source_type") or "").strip().lower()
    if source_type == "cloud139":
        return True
    text = " ".join(
        str(item.get(key) or "")
        for key in ("url", "source_url", "source_hint", "source", "provider")
    ).lower()
    return "yun.139.com" in text or "caiyun.139.com" in text or "mobile" in text or "移动" in text


def _public_resource_detail(
    cached: dict[str, Any],
    routes: dict[str, Any],
    quark_importer: Any,
    cloud139_importer: Cloud139Importer,
    sixpan_importer: Any | None = None,
    btbtla_client: Any | None = None,
    hide_full_links: bool = True,
) -> dict[str, Any]:
    service = PublicResourceDetailService(
        detect_link=detect_link,
        mask_url=_mask_share_url,
        inspect_bt=_btbtla_inspection_summary,
        inspect_resource=_inspect_public_resource,
        category_suggestion=_category_suggestion,
        format_size=_format_size,
        search_preview=_safe_search_preview,
        detail_capability=_detail_capability_for_source,
    )
    return service.build(
        cached,
        routes,
        quark_importer,
        cloud139_importer,
        sixpan_importer=sixpan_importer,
        btbtla_client=btbtla_client,
        hide_full_links=hide_full_links,
    )


def _public_resource_child_files(
    cached: dict[str, Any],
    fid: str,
    routes: dict[str, Any],
    quark_importer: Any,
    cloud139_importer: Cloud139Importer,
) -> dict[str, Any]:
    raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), dict) else {}
    source_url = str(cached.get("source_url") or raw.get("url") or raw.get("source_url") or "")
    password = str(cached.get("password") or raw.get("password") or raw.get("pwd") or "")
    title = str(cached.get("title") or raw.get("title") or raw.get("note") or "temp_check")
    link = detect_link(source_url, routes, password=password)
    source = str(link.source_type or "").strip().lower()
    if source == "cloud139":
        data = cloud139_importer.list_files(link.url, fid=fid, password=link.password, title=title)
        success = bool(isinstance(data, dict) and data.get("success", True) is not False)
        summary = _cloud139_inspection_summary(success, data)
        return {
            "success": success,
            "message": summary.get("message") or ("目录读取完成" if success else "目录读取失败"),
            "items": summary.get("items") or [],
            "summary": summary.get("summary") or {},
        }

    if source not in {"quark", "uc"}:
        return {"success": False, "message": "该来源暂不支持目录预览", "items": []}

    ok, check_data = quark_importer.check_share(link.url, title)
    if not ok:
        inspection = _quark_inspection_summary(False, check_data, fallback_size=raw.get("size") or raw.get("size_text") or "")
        return {"success": False, "message": inspection.get("message") or "资源检测失败，无法展开目录", "items": [], "inspection": inspection}

    payload = check_data if isinstance(check_data, dict) else {}
    pwd_id = _extract_quark_pwd_id(link.url) or str(_find_first_value(payload, {"pwd_id", "pwdId", "share_id", "shareId"}) or "")
    stoken = str(_find_first_value(payload, {"stoken", "share_stoken", "shareToken", "share_token"}) or "")
    if not pwd_id:
        return {"success": False, "message": "无法识别 Quark 分享 ID，暂不能展开目录", "items": []}

    data = quark_importer.list_files(pwd_id=pwd_id, fid=fid, stoken=stoken)
    success = bool(isinstance(data, dict) and data.get("success", True) is not False)
    summary = _quark_inspection_summary(success, data)
    return {
        "success": success,
        "message": summary.get("message") or ("目录读取完成" if success else "目录读取失败"),
        "items": summary.get("items") or [],
        "summary": summary.get("summary") or {},
    }


def _detail_capability_for_source(source_type: str, cloud139_importer: Cloud139Importer | None = None, sixpan_importer: Any | None = None) -> dict[str, Any]:
    source = str(source_type or "").strip().lower()
    if source in {"quark", "uc"}:
        return {"available": True, "provider": "quark", "message": "正在检测资源是否可用"}
    if source == "cloud139":
        configured = bool(cloud139_importer and cloud139_importer.configured)
        return {
            "available": configured,
            "provider": "cloud139",
            "reserved": not configured,
            "message": "正在检测资源是否支持快速入库" if configured else "快速入库服务未配置，请联系管理员",
        }
    if source == "cloud189":
        return {"available": False, "provider": "cloud189", "reserved": True, "message": "该来源暂不支持详情预览"}
    if source in {"magnet", "torrent"}:
        configured = bool(sixpan_importer and getattr(sixpan_importer, "configured", False))
        return {
            "available": configured,
            "provider": "sixpan",
            "reserved": not configured,
            "message": "查看详情后会确认是否可快速入库" if configured else "快速入库服务未授权，请联系管理员",
        }
    return {"available": False, "provider": source or "unknown", "reserved": True, "message": "该来源暂不支持详情预览"}


def _inspect_public_resource(
    link: Any,
    title: str,
    raw: dict[str, Any],
    quark_importer: Any,
    cloud139_importer: Cloud139Importer,
    sixpan_importer: Any | None = None,
) -> dict[str, Any]:
    source = str(getattr(link, "source_type", "") or "").strip().lower()
    if source in {"quark", "uc"}:
        ok, data = quark_importer.check_share(getattr(link, "url", ""), title)
        return _quark_inspection_summary(ok, data, fallback_size=raw.get("size") or raw.get("size_text") or "")
    if source == "cloud139" and cloud139_importer.configured:
        ok, data = cloud139_importer.check_share(getattr(link, "url", ""), title=title, password=str(getattr(link, "password", "") or ""))
        return _cloud139_inspection_summary(ok, data, fallback_size=raw.get("size") or raw.get("size_text") or "")
    capability = _detail_capability_for_source(source, cloud139_importer=cloud139_importer, sixpan_importer=sixpan_importer)
    return {
        "provider": capability["provider"],
        "status": "reserved",
        "success": False,
        "message": capability["message"],
        "summary": {
            "title": title,
            "total_size_text": _format_size(raw.get("size") or raw.get("size_text") or ""),
            "file_count": raw.get("file_num") or raw.get("file_count") or "",
        },
        "items": [],
    }


def _btbtla_inspection_summary(btbtla_client: Any | None, detail_url: str, *, keyword: str = "", title: str = "") -> dict[str, Any]:
    if not btbtla_client or not getattr(btbtla_client, "configured", False):
        return {
            "provider": "btbtla",
            "status": "unconfigured",
            "success": False,
            "message": "BTBTLA 搜索源未配置",
            "summary": {"title": title},
            "items": [],
        }
    try:
        data = btbtla_client.detail_resources(detail_url, keyword=keyword, title=title)
    except Exception as exc:  # noqa: BLE001
        return {
            "provider": "btbtla",
            "status": "failed",
            "success": False,
            "message": f"BTBTLA 资源列表读取失败：{exc}",
            "summary": {"title": title},
            "items": [],
        }
    items = data.get("items") if isinstance(data.get("items"), list) else []
    recommended = data.get("recommended") if isinstance(data.get("recommended"), dict) else {}
    return {
        "provider": "btbtla",
        "status": "ok" if items else "empty",
        "success": bool(items),
        "message": data.get("message") or ("已读取 BT 下载资源" if items else "未找到下载资源"),
        "summary": {
            "title": data.get("title") or title,
            "file_count": len(items),
            "recommended_id": recommended.get("id") or "",
            "recommended_title": recommended.get("title") or "",
            "episode_count": (data.get("meta") or {}).get("episode_count"),
        },
        "items": items,
        "recommended": recommended,
        "meta": data.get("meta") or {},
    }


def _preflight_public_submission(
    link: Any,
    title: str,
    raw: dict[str, Any],
    quark_importer: Any,
    cloud139_importer: Cloud139Importer,
    sixpan_importer: Any | None = None,
) -> dict[str, Any]:
    return PublicSubmissionPreflightService(
        quark_summary=_quark_inspection_summary,
        cloud139_summary=_cloud139_inspection_summary,
        detail_capability=_detail_capability_for_source,
        format_size=_format_size,
    ).check(
        link,
        title=title,
        raw=raw,
        quark_importer=quark_importer,
        cloud139_importer=cloud139_importer,
        sixpan_importer=sixpan_importer,
    )


def _quark_inspection_summary(ok: bool, data: Any, fallback_size: Any = "") -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {"data": data}
    body = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    share = body.get("share") if isinstance(body.get("share"), dict) else {}
    rows = _extract_file_rows(body)
    items = [_public_file_item(row) for row in rows[:200]]
    file_count = share.get("file_num") or share.get("file_count") or body.get("file_num") or body.get("file_count") or len(rows) or ""
    size_value = share.get("size") or share.get("total_size") or body.get("total_size") or fallback_size
    message = str(payload.get("message") or payload.get("msg") or "")
    if not message:
        message = "资源有效，可继续提交" if ok else "资源可能已失效，请换一个资源"
    return {
        "provider": "quark",
        "status": "ok" if ok else "failed",
        "success": bool(ok),
        "message": message,
        "summary": {
            "title": share.get("title") or body.get("title") or "",
            "share_status": share.get("status") or body.get("status") or "",
            "file_count": file_count,
            "total_size_text": _format_size(size_value),
        },
        "items": items,
        "raw_excerpt": {
            "success": payload.get("success"),
            "code": payload.get("code"),
            "share_status": share.get("status") or "",
            "file_count": file_count,
        },
    }


def _cloud139_inspection_summary(ok: bool, data: Any, fallback_size: Any = "") -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {"data": data}
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    share = body.get("share") if isinstance(body.get("share"), dict) else {}
    rows = _extract_file_rows(body)
    items = [_public_file_item(row) for row in rows[:200]]
    file_count = (
        share.get("file_num")
        or share.get("file_count")
        or share.get("fileCount")
        or body.get("file_num")
        or body.get("file_count")
        or body.get("fileCount")
        or len(rows)
        or ""
    )
    size_value = share.get("size") or share.get("total_size") or share.get("totalSize") or body.get("total_size") or body.get("totalSize") or fallback_size
    message = str(payload.get("message") or payload.get("msg") or "")
    if not message:
        message = "资源有效，可快速入库" if ok else "资源可能已失效，请换一个资源"
    return {
        "provider": "cloud139",
        "status": "ok" if ok else "failed",
        "success": bool(ok),
        "message": message,
        "summary": {
            "title": share.get("title") or share.get("name") or body.get("title") or body.get("name") or "",
            "share_status": share.get("status") or share.get("state") or body.get("status") or body.get("state") or "",
            "file_count": file_count,
            "total_size_text": _format_size(size_value),
        },
        "items": items,
        "raw_excerpt": {
            "success": payload.get("success"),
            "ok": payload.get("ok"),
            "code": payload.get("code"),
            "share_status": share.get("status") or share.get("state") or body.get("status") or body.get("state") or "",
            "file_count": file_count,
        },
    }


def _sixpan_parse_summary(data: Any) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {"data": data}
    body = payload
    for key in ("data", "response", "result"):
        if isinstance(payload.get(key), dict):
            body = payload[key]
            break
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    rows = body.get("task_files") or body.get("taskFiles") or body.get("files") or []
    if not isinstance(rows, list):
        rows = []
    items = [_sixpan_parse_file_item(row) for row in rows if isinstance(row, dict)]
    selectable = [item for item in items if item.get("selectable")]
    selected = [item for item in selectable if item.get("default_selected")]
    selected_videos = [item for item in selected if item.get("media_type") == "video"]
    total_size = sum(_safe_int_value(item.get("size"), 0) for item in items if not item.get("directory"))
    selected_size = sum(_safe_int_value(item.get("size"), 0) for item in selected)
    ignored = [item.get("id") for item in items if item.get("id") and not item.get("directory") and not item.get("default_selected")]
    fast_available = bool(selected_videos)
    return {
        "message": "已找到可入库内容，请选择要保存的文件" if fast_available else "暂未找到可快速入库内容，已切换为慢速入库",
        "fast_available": fast_available,
        "parse_status": "files_ready" if fast_available else "empty_files",
        "fallback_submit_all": not fast_available,
        "items": items,
        "summary": {
            "title": meta.get("name") or meta.get("file") or "",
            "file_count": len(selectable),
            "directory_count": len([item for item in items if item.get("directory")]),
            "selected_count": len(selected),
            "ignored_count": len(ignored),
            "total_size": total_size,
            "total_size_text": _format_size(total_size),
            "selected_size": selected_size,
            "selected_size_text": _format_size(selected_size),
        },
        "default_ignore_files": ignored,
        "raw_excerpt": {
            "meta": {key: meta.get(key) for key in ("identity", "name", "file", "status", "size") if key in meta},
            "task_file_count": len(rows),
        },
    }


def _sixpan_parse_file_item(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or item.get("file_name") or item.get("fileName") or item.get("path") or item.get("identity") or "未命名文件").strip()
    path = str(item.get("path") or name).strip()
    identity = str(item.get("identity") or item.get("file_identity") or item.get("fileIdentity") or path or name).strip()
    directory = bool(item.get("directory") or item.get("dir") or item.get("is_dir") or item.get("isDir"))
    size = _safe_int_value(item.get("size") or item.get("bytes_total") or item.get("bytesTotal"), 0)
    media_type = _sixpan_media_type(name or path)
    default_selected, reason = _sixpan_default_selected(name=name, path=path, directory=directory, size=size, media_type=media_type)
    selectable = bool(identity and not directory and reason in {"video", "subtitle"})
    return {
        "id": identity,
        "identity": identity,
        "name": name,
        "path": path,
        "size": size,
        "size_text": _format_size(size),
        "directory": directory,
        "is_dir": directory,
        "index": _safe_int_value(item.get("index"), 0),
        "selectable": selectable,
        "default_selected": bool(selectable and default_selected),
        "reason": reason,
        "media_type": media_type,
    }


SIXPAN_MIN_SELECTED_VIDEO_BYTES = 20 * 1024 * 1024


def _sixpan_default_selected(name: str, path: str = "", directory: bool = False, size: int = 0, media_type: str = "") -> tuple[bool, str]:
    if directory:
        return False, "directory"
    text = f"{path}/{name}".lower()
    filename = str(name or path or "").lower()
    suffix = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if any(token in text for token in ("sample", "trailer", "预告", "样片", "广告", "公众号", "最新地址", "防迷路")):
        return False, "noise"
    if media_type == "image":
        return False, "image"
    if suffix in {"mp4", "mkv", "mtv", "ts", "m2ts", "avi", "mov", "wmv", "flv", "webm", "iso", "rmvb"}:
        if 0 < int(size or 0) < SIXPAN_MIN_SELECTED_VIDEO_BYTES:
            return False, "small_video_ad"
        return True, "video"
    if suffix in {"ass", "ssa", "srt", "sup", "idx", "sub", "vtt"}:
        return True, "subtitle"
    if suffix in {"nfo", "txt", "url", "html", "htm", "jpg", "jpeg", "png", "gif", "webp", "bmp", "torrent"}:
        return False, "metadata"
    return False, "other"


def _sixpan_media_type(name: str) -> str:
    suffix = str(name or "").lower().rsplit(".", 1)[-1] if "." in str(name or "") else ""
    if suffix in {"mp4", "mkv", "mtv", "ts", "m2ts", "avi", "mov", "wmv", "flv", "webm", "iso", "rmvb"}:
        return "video"
    if suffix in {"ass", "ssa", "srt", "sup", "idx", "sub", "vtt"}:
        return "subtitle"
    if suffix in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
        return "image"
    if suffix in {"nfo", "txt", "url", "html", "htm"}:
        return "metadata"
    return "other"


def _extract_file_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidate_keys = (
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
    )
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows and any(
                item.get("file_name")
                or item.get("fileName")
                or item.get("name")
                or item.get("title")
                or item.get("fid")
                or item.get("folder_id")
                or item.get("fileId")
                or item.get("catalogID")
                or item.get("catalogId")
                or item.get("caID")
                or item.get("caId")
                or item.get("catalogName")
                or item.get("caName")
                for item in rows
            ):
                return rows
    for value in payload.values():
        if isinstance(value, dict):
            rows = _extract_file_rows(value)
            if rows:
                return rows
    return []


def _find_first_value(payload: Any, keys: set[str]) -> Any:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and value not in (None, ""):
                return value
        for value in payload.values():
            found = _find_first_value(value, keys)
            if found not in (None, ""):
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_first_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def _extract_quark_pwd_id(url: str) -> str:
    parsed = urlparse(str(url or "") if "://" in str(url or "") else f"https://{url}")
    path = parsed.path.strip("/")
    if "/s/" in f"/{path}":
        return f"/{path}".split("/s/", 1)[1].split("/", 1)[0].split("#", 1)[0]
    return ""


def _public_file_item(item: dict[str, Any]) -> dict[str, Any]:
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
        or item.get("catalogID")
        or item.get("catalogId")
        or item.get("caID")
        or item.get("caId")
        or item.get("catalogName")
        or item.get("caName")
        or item.get("hasRootFiles") is not None
        or item.get("level") is not None
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
    if "can_expand" in item:
        can_expand = bool(item.get("can_expand"))
    elif "canExpand" in item:
        can_expand = bool(item.get("canExpand"))
    elif item.get("level") is not None or item.get("hasRootFiles") is not None:
        # 部分历史适配器只返回目录概览，不是可按 fid 继续展开的文件树；
        # 这里展示预览但不误导用户继续下钻。
        can_expand = False
    else:
        can_expand = bool(is_dir and fid)
    path = str(item.get("path") or item.get("filePath") or item.get("contentPath") or item.get("coPath") or "").strip()
    share_fid_token = str(item.get("share_fid_token") or item.get("shareFidToken") or item.get("share_token") or "").strip()
    return {
        "name": str(name),
        "size_text": _format_size(item.get("size") or item.get("file_size") or item.get("fileSize") or item.get("file_size_text") or ""),
        "is_dir": is_dir,
        "can_expand": can_expand,
        "fid": fid,
        "type": "dir" if is_dir else "file",
        "path": path,
        "share_fid_token": share_fid_token,
        "level": item.get("level") if item.get("level") is not None else "",
        "has_root_files": item.get("hasRootFiles") if item.get("hasRootFiles") is not None else item.get("has_root_files"),
    }


def _format_size(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if re.search(r"[a-zA-Z一-鿿]", text):
            return text
        try:
            number = float(text)
        except ValueError:
            return text
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while number >= 1024 and unit_index < len(units) - 1:
        number /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(number)} {units[unit_index]}"
    return f"{number:.2f} {units[unit_index]}"


def _category_suggestion(item: dict[str, Any]) -> dict[str, Any]:
    text = _category_source_text(item)
    source_hint = str(item.get("source_hint") or item.get("category") or item.get("type") or "").lower()
    scores = {key: 0 for key in CATEGORY_LABELS}
    signals: list[str] = []

    def add(category: str, score: int, signal: str) -> None:
        if category in scores:
            scores[category] += score
            signals.append(signal)

    explicit_map = {
        "movie": "movie",
        "film": "movie",
        "movies": "movie",
        "tv": "tv",
        "series": "tv",
        "drama": "tv",
        "anime": "anime",
        "animation": "anime",
        "variety": "variety",
        "show": "variety",
    }
    for key, category in explicit_map.items():
        if key in source_hint:
            add(category, 5, f"来源标记包含 {key}")

    pattern_rules = [
        ("anime", 5, r"动漫|动画|番剧|日漫|国漫|新番|OVA|OAD|SP\b|剧场版动画|动画电影", "命中动漫/动画关键词"),
        ("variety", 5, r"综艺|真人秀|脱口秀|晚会|演唱会|歌手|奔跑吧|极限挑战|王牌对王牌|乘风|披荆斩棘|花儿与少年|种地吧|喜剧之王单口季|第[一二三四五六七八九十0-9]+期", "命中综艺节目关键词"),
        ("tv", 4, r"电视剧|剧集|连续剧|短剧|迷你剧|网剧|国产剧|美剧|英剧|韩剧|日剧|泰剧|港剧|台剧", "命中剧集关键词"),
        ("tv", 4, r"第[一二三四五六七八九十0-9]+季|第[一二三四五六七八九十0-9]+集|全[0-9一二三四五六七八九十]+集|更新至|完结|连载|S\d{1,2}\b|E\d{1,3}\b|EP\d{1,3}\b", "命中季/集数特征"),
        ("movie", 3, r"电影|影片|院线|蓝光|原盘|REMUX|BDRip|WEB-?DL|2160p|1080p|HDR|杜比视界", "命中电影/单体资源特征"),
    ]
    for category, score, pattern, signal in pattern_rules:
        if re.search(pattern, text, re.IGNORECASE):
            add(category, score, signal)

    if re.search(r"(19|20)\d{2}", text) and not re.search(r"第[一二三四五六七八九十0-9]+[季集期]|S\d{1,2}\b|E\d{1,3}\b|更新至", text, re.IGNORECASE):
        add("movie", 2, "标题包含年份且未发现季/集数特征")

    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    best_key, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    if best_score <= 0:
        return {
            "key": "movie",
            "label": CATEGORY_LABELS["movie"],
            "confidence": 0.35,
            "reason": "未识别到明显剧集、综艺或动漫特征，默认按电影处理；用户可手动切换分类",
            "signals": [],
            "source": "heuristic",
        }
    confidence = min(0.96, 0.45 + best_score * 0.07 + max(0, best_score - second_score) * 0.04)
    return {
        "key": best_key,
        "label": CATEGORY_LABELS.get(best_key, best_key),
        "confidence": round(confidence, 2),
        "reason": "；".join(signals[:3]) or "根据标题与来源信息推断",
        "signals": signals[:6],
        "source": "heuristic",
    }


def _category_source_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("name"),
        item.get("note"),
        item.get("content"),
        item.get("keyword"),
        item.get("source_hint"),
        item.get("category"),
    ]
    raw = item.get("raw_data")
    if isinstance(raw, dict):
        parts.extend([raw.get("title"), raw.get("note"), raw.get("content"), raw.get("category"), raw.get("type")])
    return " ".join(str(part or "") for part in parts)


def _safe_search_preview(raw: dict[str, Any], hide_full_links: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed_keys = [
        "title",
        "note",
        "content",
        "datetime",
        "created_at",
        "time",
        "size",
        "source",
        "source_hint",
        "provider",
        "matched_keyword",
        "quality_tags",
        "duplicate_sources",
        "duplicate_count",
        "poster",
        "cover",
        "image_url",
        "source_origin",
        "referer",
    ]
    preview = {key: raw.get(key) for key in allowed_keys if raw.get(key) not in (None, "")}
    for key in ("url", "link", "share_url", "source_url"):
        if raw.get(key):
            preview[f"{key}_masked" if hide_full_links else key] = _mask_share_url(str(raw.get(key))) if hide_full_links else raw.get(key)
    return preview


def _mask_share_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("magnet:"):
        return "magnet:?xt=***"
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    if not host:
        return "***"
    return f"{host}/***"


def _public_categories(categories: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "label": value.get("label") or key,
        }
        for key, value in categories.items()
    }


def _public_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"done", "success"}:
        return "入库完成"
    if normalized in {"failed", "error"}:
        return "处理失败"
    if normalized == "rejected":
        return "未通过"
    if normalized == "cancelled":
        return "已取消"
    if normalized == "unsupported":
        return "暂不支持"
    if normalized in {"pending_review", "review", "waiting_review", "pending_organizer_review"}:
        return "等待处理"
    return "处理中"


def _public_request_message_for_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"done", "success"}:
        return "系统已完成处理，可在影视库中搜索查看。"
    if normalized in {"failed", "error"}:
        return "当前提交未完成，请稍后重试或联系管理员。"
    if normalized == "rejected":
        return "提交未通过，如有疑问请联系管理员。"
    if normalized == "cancelled":
        return "提交已取消。"
    if normalized == "unsupported":
        return "当前资源暂不支持自动入库。"
    if normalized in {"pending_review", "review", "waiting_review", "pending_organizer_review"}:
        return "系统已收到你的入库请求，等待管理员处理。"
    return "系统已收到你的入库请求，正在处理。"


def _public_request_response(guest_request: dict[str, Any] | None) -> dict[str, Any]:
    item = guest_request if isinstance(guest_request, dict) else {}
    if not item:
        return {}
    status = str(item.get("status") or "")
    return {
        "token": item.get("request_token"),
        "title": item.get("title"),
        "category": item.get("category"),
        "category_label": item.get("category_label"),
        "status": item.get("public_status") or _public_status(status),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "message": _public_request_message_for_status(status),
    }


def _public_submit_message(result: dict[str, Any]) -> str:
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    status = str(job.get("status") or "").lower()
    if not result.get("success", True):
        return _public_request_message_for_status(status or "failed")
    if status in {"done", "success"}:
        return "提交成功，系统已完成处理。"
    return "提交成功，系统已收到请求，请保存提交编号。"


def _submission_mode(config: dict[str, Any]) -> str:
    return _normalize_submission_mode(config.get("submission", {}).get("mode"))


def _normalize_submission_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in {"auto", "review", "mixed"} else "auto"


def _auto_submit_allowed(mode: str, link: dict[str, Any]) -> bool:
    if mode == "auto":
        return True
    if mode == "review":
        return False
    if mode == "mixed":
        return bool(link.get("supported"))
    return True


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _short_text(value: Any, limit: int = 40) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _new_public_id() -> str:
    return f"RS-{_random_code(10)}"


def _new_request_token() -> str:
    return f"RQ-{datetime.now().strftime('%y%m%d')}-{_random_code(6)}"


def _random_code(length: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _hash_client_ip(value: str, salt: str = "") -> str:
    text = str(value or "").split(",", 1)[0].strip()
    if not text:
        return ""
    return hashlib.sha256(f"{salt}:{text}".encode("utf-8")).hexdigest()


def _public_cached_item(cached: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cached:
        return None
    return {
        "public_id": cached.get("public_id"),
        "title": cached.get("title"),
        "source_type": cached.get("source_type"),
        "source_url_hash": cached.get("source_url_hash"),
        "expires_at": cached.get("expires_at"),
    }


def _public_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_selection = payload.get("sixpan_selection") if isinstance(payload.get("sixpan_selection"), dict) else {}
    ignore_source = payload.get("ignore_files") if "ignore_files" in payload else raw_selection.get("ignore_files")
    ignore_files = _safe_public_string_list(ignore_source, max_items=2000, max_length=512)
    sixpan_selection = _safe_public_sixpan_selection(raw_selection)
    if sixpan_selection or ignore_files or raw_selection:
        sixpan_selection = {
            **sixpan_selection,
            "ignore_files": ignore_files,
            "ignored_count": len(ignore_files),
        }
    quark_selection = _safe_public_quark_selection(payload.get("quark_selection"))
    cloud139_selection = _safe_public_cloud139_selection(payload.get("cloud139_selection"))
    return {
        "public_id": payload.get("public_id") or payload.get("resource_id") or "",
        "category": payload.get("category") or "",
        "preferred_title": payload.get("preferred_title") or payload.get("title") or "",
        "note": payload.get("note") or "",
        "has_manual_url": bool(payload.get("url")),
        "ignore_files": ignore_files,
        "sixpan_selection": sixpan_selection,
        "quark_selection": quark_selection,
        "cloud139_selection": cloud139_selection,
    }


def _guest_safe_job_result(result: dict[str, Any]) -> dict[str, Any]:
    job = result.get("job") or {}
    return {
        "success": bool(result.get("success", True)),
        "created": bool(result.get("created", False)),
        "message": result.get("message") or "",
        "job_id": job.get("id"),
        "job_status": job.get("status"),
        "target_path": job.get("target_path"),
    }


def _rclone_callback_level(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"failed", "error", "upload_error", "upload_exception", "auth_expired", "auth_config_error", "rapid_miss"}:
        return "error"
    if normalized in {"skipped", "skipped_existing"}:
        return "warn"
    return "info"
