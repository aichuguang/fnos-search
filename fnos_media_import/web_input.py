from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .database import Database
from .organizer.openlist_client import VIDEO_EXTENSIONS
from .process_role import role_runs


class PublicInputError(ValueError):
    pass


def _payload_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_bool(settings: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in settings:
        return default
    value = settings.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _merge_raw_data(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    if isinstance(current, dict):
        merged = dict(current)
    elif current in (None, ""):
        merged = {}
    else:
        merged = {"previous_raw_data": current}
    merged.update(patch)
    return merged


def _read_jsonl_tail(path_value: Any, limit: int) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"raw": text}
        if isinstance(data, dict):
            items.append(data)
    return items


def _config_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _default_secret_key(value: Any) -> bool:
    text = str(value or "").strip()
    lowered = text.lower()
    return not text or lowered.startswith("change-me") or lowered in {"secret", "default", "please-change-me"}


def _strict_security_enabled(config: dict[str, Any] | None = None) -> bool:
    raw = config if isinstance(config, dict) else {}
    security = raw.get("security") if isinstance(raw.get("security"), dict) else {}
    app_section = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    explicit = (
        os.getenv("FNOS_SECURITY_STRICT")
        or os.getenv("SECURITY_STRICT")
        or security.get("strict")
        or security.get("strict_mode")
    )
    if explicit not in (None, ""):
        return str(explicit).strip().lower() in {"1", "true", "yes", "on", "prod", "production"}
    env_name = str(os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or os.getenv("ENV") or app_section.get("env") or "").strip().lower()
    if env_name in {"prod", "production", "release"}:
        return True
    return False


def _config_int(config: dict[str, Any], key: str, default: int = 0) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _csv_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _limited_text(value: Any, label: str, max_length: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PublicInputError(f"{label}不能为空")
    if max_length > 0 and len(text) > max_length:
        raise PublicInputError(f"{label}长度不能超过 {max_length} 个字符")
    return text


def _clip_text(value: Any, max_length: int) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length > 0 else text


def _sanitize_sources(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    items = value if isinstance(value, list) else [value]
    if len(items) > 20:
        raise PublicInputError("搜索源数量不能超过 20 个")
    result = []
    for item in items:
        text = _limited_text(item, "搜索源", 32)
        if text:
            result.append(text)
    return result or None


def _extract_url_candidate(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("magnet:?"):
        return text.split()[0]
    match = re.search(r"https?://[^\s]+", text, flags=re.IGNORECASE)
    if match:
        return match.group(0).rstrip("。；;，,")
    return text


def _validate_public_url(value: Any, security_config: dict[str, Any]) -> str:
    max_length = _config_int(security_config, "max_url_length", 2048)
    raw = _limited_text(value, "资源链接", max_length, required=True)
    url = _extract_url_candidate(raw)
    lower = url.lower()
    allowed_schemes = {item.lower() for item in _csv_values(security_config.get("allowed_url_schemes") or ["http", "https", "magnet"])}

    if lower.startswith("magnet:?"):
        if "magnet" not in allowed_schemes:
            raise PublicInputError("当前不允许提交磁链")
        if "xt=" not in lower:
            raise PublicInputError("磁链格式不正确")
        return url

    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise PublicInputError(f"不允许的链接协议：{scheme}")
    if scheme not in {"http", "https"}:
        raise PublicInputError("当前只允许 http、https 或 magnet 链接")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise PublicInputError("资源链接缺少域名")
    _validate_public_host(host, security_config)
    return url


def _validate_public_host(host: str, security_config: dict[str, Any]) -> None:
    allowed_domains = [item.lower().strip(".") for item in _csv_values(security_config.get("allowed_share_domains"))]
    if allowed_domains and not any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
        raise PublicInputError("该分享域名不在允许列表中")

    blocked_names = {"localhost", "localhost.localdomain"}
    if host in blocked_names or host.endswith(".local"):
        raise PublicInputError("不允许提交本地域名链接")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host and not allowed_domains:
            raise PublicInputError("资源链接域名格式不正确")
        return

    if _config_bool(security_config, "block_private_hosts", True) and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
    ):
        raise PublicInputError("不允许提交内网或本机地址")


def _safe_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(min_value, min(max_value, number))


def _safe_int_value(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _public_import_worker_result(outcome: Any) -> dict[str, Any]:
    """Keep durable-worker outcome semantics when wrapping public imports.

    The public coordinator returns a rich import result.  The worker wrapper
    intentionally exposes only a compact summary, but it must not discard
    retry/defer/business-failure markers or the runtime would incorrectly mark
    a failed submission as completed.
    """

    source = getattr(outcome, "result", None)
    source = source if isinstance(source, dict) else {}
    preserved_keys = (
        "worker_outcome",
        "outcome",
        "retryable",
        "deferred",
        "retry_after_seconds",
        "delay_seconds",
        "terminal",
        "cancelled",
        "compensation_failed",
        "skipped",
        "message",
        "error",
        "error_message",
    )
    result = {key: source[key] for key in preserved_keys if key in source}
    result["success"] = bool(source.get("success", True))
    job = getattr(outcome, "job", None)
    job = job if isinstance(job, dict) else {}
    result.update(
        {
            "job_id": job.get("id"),
            "public_status": str(getattr(outcome, "public_status", "") or ""),
            "bind_outcome": str(getattr(outcome, "bind_outcome", "") or ""),
        }
    )
    rclone_start = getattr(outcome, "rclone_start", None)
    if rclone_start is not None:
        result["rclone_start"] = rclone_start
    return result


def _public_import_compensation_retry_job_id(task: dict[str, Any]) -> int:
    previous_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    if not previous_result.get("compensation_failed") or not previous_result.get("retryable"):
        return 0
    try:
        job_id = int(previous_result.get("job_id") or 0)
    except (TypeError, ValueError):
        return 0
    return job_id if job_id > 0 else 0


def _video_file_paths(values: list[str]) -> list[str]:
    return [
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
        and Path(str(value or "").replace("\\", "/")).suffix.lower() in VIDEO_EXTENSIONS
    ]


def _worker_dispatch_enabled_for_role(worker_config: dict[str, Any], process_role: str) -> bool:
    """Enable durable producers automatically when this process cannot execute them.

    ``web`` and ``scheduler`` runtimes suspend Organizer background timers by
    design.  Selecting either role therefore also opts the process into durable
    dispatch, while the legacy ``all`` role continues to honor the explicit
    configuration switch.
    """

    return not role_runs(process_role, "worker") or _config_bool(
        worker_config,
        "durable_dispatch_enabled",
        False,
    )


def _recent_business_events(
    db: Database,
    limit: int = 100,
    *,
    offset: int = 0,
    keyword: str = "",
    source: str = "",
    job_id: int | None = None,
) -> dict[str, Any]:
    base_sql = """
        WITH business_events AS (
            SELECT
                gr.created_at AS created_at,
                'info' AS level,
                'guest' AS source,
                '访客申请' AS source_label,
                '访客提交入库申请' AS message,
                NULL AS raw_data,
                gr.request_token AS ref,
                gr.title AS title,
                gr.job_id AS job_id,
                gr.id AS request_id,
                'guest_request_created' AS event_type,
                gr.id AS event_id
            FROM guest_requests gr
            UNION ALL
            SELECT
                ge.created_at AS created_at,
                ge.level AS level,
                'guest' AS source,
                '访客申请' AS source_label,
                ge.message AS message,
                ge.raw_data AS raw_data,
                gr.request_token AS ref,
                gr.title AS title,
                gr.job_id AS job_id,
                gr.id AS request_id,
                'guest_request_event' AS event_type,
                ge.id AS event_id
            FROM guest_request_events ge
            JOIN guest_requests gr ON gr.id = ge.request_id
            UNION ALL
            SELECT
                ij.created_at AS created_at,
                'info' AS level,
                'job' AS source,
                '入库任务' AS source_label,
                '入库任务已创建' AS message,
                NULL AS raw_data,
                ('#' || ij.id) AS ref,
                ij.title AS title,
                ij.id AS job_id,
                NULL AS request_id,
                'job_created' AS event_type,
                ij.id AS event_id
            FROM import_jobs ij
            UNION ALL
            SELECT
                je.created_at AS created_at,
                je.level AS level,
                'job' AS source,
                '入库任务' AS source_label,
                je.message AS message,
                je.raw_data AS raw_data,
                ('#' || ij.id) AS ref,
                ij.title AS title,
                ij.id AS job_id,
                NULL AS request_id,
                'job_event' AS event_type,
                je.id AS event_id
            FROM job_events je
            JOIN import_jobs ij ON ij.id = je.job_id
            UNION ALL
            SELECT
                ue.created_at AS created_at,
                ue.level AS level,
                'update' AS source,
                '定时追更' AS source_label,
                ue.message AS message,
                ue.raw_data AS raw_data,
                ('#' || us.id) AS ref,
                us.title AS title,
                NULL AS job_id,
                NULL AS request_id,
                'update_event' AS event_type,
                ue.id AS event_id
            FROM update_events ue
            JOIN update_subscriptions us ON us.id = ue.subscription_id
            UNION ALL
            SELECT
                rfe.created_at AS created_at,
                rfe.level AS level,
                'rclone' AS source,
                'rclone 搬运' AS source_label,
                COALESCE(NULLIF(rfe.message, ''), ('文件搬运：' || rfe.filename)) AS message,
                rfe.raw_data AS raw_data,
                ('#' || ij.id) AS ref,
                ij.title AS title,
                ij.id AS job_id,
                NULL AS request_id,
                'rclone_file_event' AS event_type,
                rfe.id AS event_id
            FROM rclone_file_events rfe
            JOIN import_jobs ij ON ij.id = rfe.job_id
            UNION ALL
            SELECT
                ot.created_at AS created_at,
                CASE WHEN ot.status IN ('failed', 'error') THEN 'error' ELSE 'info' END AS level,
                'organizer' AS source,
                '文件整理' AS source_label,
                '文件整理任务已创建' AS message,
                NULL AS raw_data,
                CASE WHEN ot.job_id IS NULL THEN ('整理 #' || ot.id) ELSE ('#' || ot.job_id) END AS ref,
                COALESCE(ot.title, ot.openlist_root_path) AS title,
                ot.job_id AS job_id,
                ot.request_id AS request_id,
                'organizer_task_created' AS event_type,
                ot.id AS event_id
            FROM organizer_tasks ot
            UNION ALL
            SELECT
                COALESCE(orun.finished_at, orun.started_at) AS created_at,
                CASE WHEN orun.status = 'failed' THEN 'error' ELSE 'info' END AS level,
                'organizer' AS source,
                '文件整理' AS source_label,
                ('整理运行 ' || orun.status) AS message,
                orun.summary AS raw_data,
                CASE WHEN ot.job_id IS NULL THEN ('整理 #' || ot.id) ELSE ('#' || ot.job_id) END AS ref,
                COALESCE(ot.title, ot.openlist_root_path) AS title,
                ot.job_id AS job_id,
                ot.request_id AS request_id,
                'organizer_run' AS event_type,
                orun.id AS event_id
            FROM organizer_runs orun
            JOIN organizer_tasks ot ON ot.id = orun.task_id
        )
    """
    filters: list[str] = []
    values: list[Any] = []
    normalized_source = str(source or "").strip().lower()
    normalized_keyword = str(keyword or "").strip()
    if normalized_source:
        filters.append("source = ?")
        values.append(normalized_source)
    if job_id is not None:
        filters.append("job_id = ?")
        values.append(int(job_id))
    if normalized_keyword:
        filters.append(
            "(message LIKE ? OR COALESCE(ref, '') LIKE ? OR COALESCE(title, '') LIKE ? OR CAST(COALESCE(job_id, '') AS TEXT) LIKE ?)"
        )
        pattern = f"%{normalized_keyword}%"
        values.extend([pattern, pattern, pattern, pattern])
    where_sql = f" WHERE {' AND '.join(filters)}" if filters else ""
    safe_limit = max(1, min(int(limit or 100), 500))
    safe_offset = max(0, int(offset or 0))

    with db.connect() as conn:
        total_row = conn.execute(f"{base_sql} SELECT COUNT(*) AS total FROM business_events{where_sql}", values).fetchone()
        rows = conn.execute(
            f"{base_sql} SELECT * FROM business_events{where_sql} ORDER BY created_at DESC, event_id DESC LIMIT ? OFFSET ?",
            [*values, safe_limit, safe_offset],
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("raw_data"):
            try:
                item["raw_data"] = json.loads(item["raw_data"])
            except (TypeError, json.JSONDecodeError):
                pass
        items.append(item)
    return {"items": items, "total": int(total_row["total"] if total_row else 0)}


def _task_log_summaries(
    db: Database,
    limit: int = 50,
    *,
    offset: int = 0,
    keyword: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    stats_sql = """
        WITH guest_stats AS (
            SELECT
                gr.job_id AS job_id,
                COUNT(DISTINCT gr.id) + COUNT(gre.id) AS event_count,
                SUM(CASE WHEN gre.level = 'error' THEN 1 ELSE 0 END) AS error_count,
                MAX(COALESCE(gre.created_at, gr.updated_at, gr.created_at)) AS latest_at
            FROM guest_requests gr
            LEFT JOIN guest_request_events gre ON gre.request_id = gr.id
            WHERE gr.job_id IS NOT NULL
            GROUP BY gr.job_id
        ),
        job_stats AS (
            SELECT
                job_id,
                COUNT(*) AS event_count,
                SUM(CASE WHEN level = 'error' THEN 1 ELSE 0 END) AS error_count,
                MAX(created_at) AS latest_at
            FROM job_events
            GROUP BY job_id
        ),
        rclone_stats AS (
            SELECT
                job_id,
                COUNT(*) AS event_count,
                SUM(CASE WHEN level = 'error' OR status IN ('failed', 'error', 'upload_error', 'upload_exception') THEN 1 ELSE 0 END) AS error_count,
                MAX(created_at) AS latest_at
            FROM rclone_file_events
            WHERE job_id IS NOT NULL
            GROUP BY job_id
        ),
        organizer_events AS (
            SELECT job_id, created_at, CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END AS is_error
            FROM organizer_tasks
            WHERE job_id IS NOT NULL
            UNION ALL
            SELECT ot.job_id, COALESCE(orun.finished_at, orun.started_at), CASE WHEN orun.status IN ('failed', 'error') THEN 1 ELSE 0 END
            FROM organizer_runs orun
            JOIN organizer_tasks ot ON ot.id = orun.task_id
            WHERE ot.job_id IS NOT NULL
            UNION ALL
            SELECT ot.job_id, COALESCE(op.updated_at, op.created_at), CASE WHEN op.status IN ('failed', 'error') THEN 1 ELSE 0 END
            FROM organizer_operations op
            JOIN organizer_tasks ot ON ot.id = op.task_id
            WHERE ot.job_id IS NOT NULL
        ),
        organizer_stats AS (
            SELECT job_id, COUNT(*) AS event_count, SUM(is_error) AS error_count, MAX(created_at) AS latest_at
            FROM organizer_events
            GROUP BY job_id
        )
    """
    filters: list[str] = []
    values: list[Any] = []
    normalized_keyword = str(keyword or "").strip()
    normalized_status = str(status or "").strip().lower()
    normalized_date_from = str(date_from or "").strip()[:10]
    normalized_date_to = str(date_to or "").strip()[:10]
    if normalized_keyword:
        filters.append("(CAST(ij.id AS TEXT) LIKE ? OR ij.title LIKE ? OR ij.source_url LIKE ?)")
        pattern = f"%{normalized_keyword.lstrip('#')}%"
        values.extend([pattern, f"%{normalized_keyword}%", f"%{normalized_keyword}%"])
    if normalized_status:
        filters.append("ij.status = ?")
        values.append(normalized_status)
    if normalized_date_from:
        filters.append("substr(ij.created_at, 1, 10) >= ?")
        values.append(normalized_date_from)
    if normalized_date_to:
        filters.append("substr(ij.created_at, 1, 10) <= ?")
        values.append(normalized_date_to)
    where_sql = f" WHERE {' AND '.join(filters)}" if filters else ""
    safe_limit = max(1, min(int(limit or 50), 200))
    safe_offset = max(0, int(offset or 0))
    select_sql = f"""
        {stats_sql}
        SELECT
            ij.id,
            ij.title,
            ij.category,
            ij.category_label,
            ij.source_type,
            ij.status,
            ij.error_message,
            ij.created_at,
            ij.updated_at,
            CASE WHEN ij.status IN ('done', 'success', 'completed', 'failed', 'error', 'cancelled') THEN ij.updated_at ELSE NULL END AS finished_at,
            1 + COALESCE(gs.event_count, 0) + COALESCE(js.event_count, 0) + COALESCE(rs.event_count, 0) + COALESCE(os.event_count, 0) AS log_count,
            CASE WHEN ij.status IN ('failed', 'error') THEN 1 ELSE 0 END
                + COALESCE(gs.error_count, 0) + COALESCE(js.error_count, 0) + COALESCE(rs.error_count, 0) + COALESCE(os.error_count, 0) AS error_count,
            MAX(
                COALESCE(ij.updated_at, ''),
                COALESCE(gs.latest_at, ''),
                COALESCE(js.latest_at, ''),
                COALESCE(rs.latest_at, ''),
                COALESCE(os.latest_at, '')
            ) AS latest_log_at
        FROM import_jobs ij
        LEFT JOIN guest_stats gs ON gs.job_id = ij.id
        LEFT JOIN job_stats js ON js.job_id = ij.id
        LEFT JOIN rclone_stats rs ON rs.job_id = ij.id
        LEFT JOIN organizer_stats os ON os.job_id = ij.id
        {where_sql}
        ORDER BY ij.id DESC
        LIMIT ? OFFSET ?
    """
    with db.connect() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS total FROM import_jobs ij{where_sql}", values).fetchone()
        rows = conn.execute(select_sql, [*values, safe_limit, safe_offset]).fetchall()
        items = [dict(row) for row in rows]
        item_by_job_id = {int(item["id"]): item for item in items}
        if item_by_job_id:
            placeholders = ", ".join("?" for _ in item_by_job_id)
            job_ids = list(item_by_job_id)
            request_to_job = {
                int(row["id"]): int(row["job_id"])
                for row in conn.execute(
                    f"SELECT id, job_id FROM guest_requests WHERE job_id IN ({placeholders})",
                    job_ids,
                ).fetchall()
            }
            organizer_to_job = {
                int(row["id"]): int(row["job_id"])
                for row in conn.execute(
                    f"SELECT id, job_id FROM organizer_tasks WHERE job_id IN ({placeholders})",
                    job_ids,
                ).fetchall()
            }
            worker_rows = conn.execute(
                "SELECT id, payload, status, created_at, updated_at, started_at, completed_at FROM worker_tasks ORDER BY id ASC"
            ).fetchall()
            for worker_row in worker_rows:
                try:
                    payload = json.loads(worker_row["payload"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                try:
                    worker_job_id = int(payload.get("job_id") or 0)
                    request_id = int(payload.get("guest_request_id") or 0)
                    organizer_task_id = int(payload.get("task_id") or 0)
                except (TypeError, ValueError):
                    continue
                related_job_id = (
                    worker_job_id
                    if worker_job_id in item_by_job_id
                    else request_to_job.get(request_id) or organizer_to_job.get(organizer_task_id)
                )
                if related_job_id not in item_by_job_id:
                    continue
                item = item_by_job_id[int(related_job_id)]
                item["log_count"] = int(item.get("log_count") or 0) + 1
                if str(worker_row["status"] or "") == "failed":
                    item["error_count"] = int(item.get("error_count") or 0) + 1
                worker_at = (
                    worker_row["completed_at"]
                    or worker_row["updated_at"]
                    or worker_row["started_at"]
                    or worker_row["created_at"]
                    or ""
                )
                if str(worker_at) > str(item.get("latest_log_at") or ""):
                    item["latest_log_at"] = worker_at
    return {
        "items": items,
        "total": int(total_row["total"] if total_row else 0),
    }


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    sensitive_keys = {
        "password",
        "token",
        "default_token",
        "secret_key",
        "client_secret",
        "clientsecret",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "device_code",
        "devicecode",
        "authorization",
        "app_callback_url",
    }

    def redact(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {child_key: redact(child_value, child_key) for child_key, child_value in value.items()}
        if key.lower() in sensitive_keys:
            return "***" if value else ""
        return value

    return redact(config)
