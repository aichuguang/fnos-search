"""通知事件定义与默认消息模板。

事件发射点、幂等键、严重级别、默认主题和正文集中在这里，便于在现有
transition 点直接调用发射器，而不必各自拼装文案。
"""

from __future__ import annotations

import html
from typing import Any

EVENT_GUEST_NEW = "guest_new"
EVENT_GUEST_REVIEW_REQUIRED = "guest_review_required"
EVENT_GUEST_EMAIL_VERIFY = "guest_email_verify"
EVENT_GUEST_APPROVED = "guest_approved"
EVENT_GUEST_REJECTED = "guest_rejected"
EVENT_ORGANIZER_REVIEW_REQUIRED = "organizer_review_required"
EVENT_JOB_FAILED = "job_failed"
EVENT_JOB_DONE = "job_done"
EVENT_DIGEST_DAILY = "digest_daily"

ALL_EVENTS = (
    EVENT_GUEST_NEW,
    EVENT_GUEST_REVIEW_REQUIRED,
    EVENT_GUEST_EMAIL_VERIFY,
    EVENT_GUEST_APPROVED,
    EVENT_GUEST_REJECTED,
    EVENT_ORGANIZER_REVIEW_REQUIRED,
    EVENT_JOB_FAILED,
    EVENT_JOB_DONE,
    EVENT_DIGEST_DAILY,
)

EVENT_LABELS: dict[str, str] = {
    EVENT_GUEST_NEW: "新申请已创建",
    EVENT_GUEST_REVIEW_REQUIRED: "新申请待审核",
    EVENT_GUEST_EMAIL_VERIFY: "访客邮箱验证",
    EVENT_GUEST_APPROVED: "申请审核通过",
    EVENT_GUEST_REJECTED: "申请审核未通过",
    EVENT_ORGANIZER_REVIEW_REQUIRED: "OpenList 整理待审核",
    EVENT_JOB_FAILED: "任务失败",
    EVENT_JOB_DONE: "任务完成",
    EVENT_DIGEST_DAILY: "每日汇总",
}

# 事件 → 默认启用渠道。channel 名称：email / webhook。
DEFAULT_RULES: dict[str, list[str]] = {
    EVENT_GUEST_NEW: [],
    EVENT_GUEST_REVIEW_REQUIRED: ["email", "webhook"],
    EVENT_GUEST_EMAIL_VERIFY: ["guest_email"],
    EVENT_GUEST_APPROVED: ["webhook", "guest_email"],
    EVENT_GUEST_REJECTED: ["webhook", "guest_email"],
    EVENT_ORGANIZER_REVIEW_REQUIRED: ["email", "webhook"],
    EVENT_JOB_FAILED: ["email", "webhook", "guest_email"],
    EVENT_JOB_DONE: ["webhook", "guest_email"],
    EVENT_DIGEST_DAILY: ["email"],
}

CHANNEL_EMAIL = "email"
CHANNEL_WEBHOOK = "webhook"
CHANNEL_GUEST_EMAIL = "guest_email"
ALL_CHANNELS = (CHANNEL_EMAIL, CHANNEL_WEBHOOK, CHANNEL_GUEST_EMAIL)

_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    EVENT_GUEST_NEW: ("request_id", "title", "category", "category_label", "source_type"),
    EVENT_GUEST_REVIEW_REQUIRED: ("request_id", "request_token", "title", "category", "category_label", "source_type", "reason"),
    EVENT_GUEST_EMAIL_VERIFY: ("request_id", "request_token", "title"),
    EVENT_GUEST_APPROVED: ("request_id", "request_token", "title", "category", "category_label"),
    EVENT_GUEST_REJECTED: ("request_id", "request_token", "title", "category", "reason"),
    EVENT_ORGANIZER_REVIEW_REQUIRED: (
        "task_id",
        "task_revision",
        "job_id",
        "title",
        "category",
        "issue_count",
        "reason",
    ),
    EVENT_JOB_FAILED: ("request_id", "request_token", "job_id", "title", "stage", "error"),
    EVENT_JOB_DONE: ("request_id", "request_token", "job_id", "title"),
    EVENT_DIGEST_DAILY: ("date", "new_count", "pending_review_count", "done_count", "failed_count"),
}


def event_severity(event_type: str) -> str:
    if event_type == EVENT_JOB_FAILED:
        return "error"
    if event_type == EVENT_ORGANIZER_REVIEW_REQUIRED:
        return "warning"
    return "info"


def idempotency_key(event_type: str, ref: str) -> str:
    return f"notify:{event_type}:{ref}"


def sanitize_context(event_type: str, context: dict[str, Any]) -> dict[str, Any]:
    allowed = _CONTEXT_FIELDS.get(event_type, ("message",))
    result: dict[str, Any] = {}
    for key in allowed:
        value = context.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            limit = 500 if key in {"error", "reason", "message"} else 300
            result[key] = _clip(str(value), limit) if isinstance(value, str) else value
    return result


def _admin_url(public_base_url: str, context: dict[str, Any]) -> str:
    base = str(public_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    explicit = context.get("admin_url")
    if explicit:
        return str(explicit)
    return f"{base}/admin"


def _source_label(source_type: Any) -> str:
    labels = {
        "quark": "夸克",
        "uc": "UC",
        "cloud139": "移动云盘",
        "cloud189": "天翼云盘",
        "magnet": "磁力",
        "torrent": "种子",
        "aliyun": "阿里云盘",
        "baidu": "百度网盘",
        "sixpan": "六盘",
        "btbtla": "BTBTLa",
    }
    return labels.get(str(source_type or "").strip(), str(source_type or "未知"))


def build_email(event_type: str, context: dict[str, Any], public_base_url: str = "") -> tuple[str, str]:
    """返回 (subject, plain_text_body)。所有链接只指向已登录的后台页面。"""
    url = _admin_url(public_base_url, context)
    if event_type in {EVENT_GUEST_NEW, EVENT_GUEST_REVIEW_REQUIRED}:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】新申请待审核：{title}",
            (
                "收到一条新的入库申请，等待审核。\n"
                f"标题：{title}\n"
                f"分类：{context.get('category_label') or context.get('category') or '-'}\n"
                f"来源：{_source_label(context.get('source_type'))}\n"
                f"申请编号：{context.get('request_id') or '-'}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    if event_type == EVENT_GUEST_APPROVED:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】申请已审核通过：{title}",
            (
                f"申请「{title}」已审核通过，正在进入入库流程。\n"
                f"申请编号：{context.get('request_id') or '-'}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    if event_type == EVENT_GUEST_REJECTED:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】申请审核未通过：{title}",
            (
                f"申请「{title}」未通过审核。\n"
                f"说明：{context.get('reason') or '未提供'}\n"
                f"申请编号：{context.get('request_id') or '-'}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    if event_type == EVENT_ORGANIZER_REVIEW_REQUIRED:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】OpenList 整理待审核：{title}",
            (
                "Organizer 生成的整理计划需要管理员确认。\n"
                f"标题：{title}\n"
                f"Organizer 任务：{context.get('task_id') or '-'}\n"
                f"入库任务：{context.get('job_id') or '-'}\n"
                f"异常项目：{context.get('issue_count', 0)}\n"
                f"原因：{context.get('reason') or '需要人工确认'}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    if event_type == EVENT_JOB_FAILED:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】任务失败：{title}",
            (
                f"入库任务执行失败，请到后台查看详情。\n"
                f"任务编号：{context.get('job_id') or '-'}\n"
                f"标题：{title}\n"
                f"失败阶段：{context.get('stage') or '-'}\n"
                f"错误：{_clip(str(context.get('error') or ''), 300)}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    if event_type == EVENT_JOB_DONE:
        title = str(context.get("title") or "未命名资源")
        return (
            f"【影视搜索】任务完成：{title}",
            f"入库任务已完成。\n任务编号：{context.get('job_id') or '-'}\n标题：{title}\n",
        )
    if event_type == EVENT_DIGEST_DAILY:
        return (
            "【影视搜索】每日汇总",
            (
                "过去 24 小时入库情况：\n"
                f"新申请：{context.get('new_count', 0)}\n"
                f"待审核：{context.get('pending_review_count', 0)}\n"
                f"完成：{context.get('done_count', 0)}\n"
                f"失败：{context.get('failed_count', 0)}\n"
                + (f"后台入口：{url}\n" if url else "")
            ),
        )
    return (
        f"【影视搜索】通知",
        f"{context.get('message') or ''}\n" + (f"后台入口：{url}\n" if url else ""),
    )


def build_email_html(event_type: str, context: dict[str, Any], public_base_url: str = "") -> str:
    """构建邮箱兼容的管理员 HTML 正文；所有动态内容在渲染时转义。"""
    url = _admin_url(public_base_url, context)
    title = str(context.get("title") or "未命名资源")
    if event_type in {EVENT_GUEST_NEW, EVENT_GUEST_REVIEW_REQUIRED}:
        return _render_email_html(
            eyebrow="新申请待审核",
            heading=title,
            summary="收到一条新的入库申请，请及时进入后台确认资源信息。",
            tone="warning",
            rows=(
                ("分类", context.get("category_label") or context.get("category") or "-"),
                ("来源", _source_label(context.get("source_type"))),
                ("申请编号", context.get("request_id") or "-"),
            ),
            action_url=url,
            action_label="进入后台审核",
        )
    if event_type == EVENT_GUEST_APPROVED:
        return _render_email_html(
            eyebrow="申请已通过",
            heading=title,
            summary="申请已经审核通过，系统将继续执行后续入库流程。",
            tone="success",
            rows=(("申请编号", context.get("request_id") or "-"),),
            action_url=url,
            action_label="查看处理进度",
        )
    if event_type == EVENT_GUEST_REJECTED:
        return _render_email_html(
            eyebrow="申请未通过",
            heading=title,
            summary="这条申请未通过审核，请查看说明并按需处理。",
            tone="danger",
            rows=(
                ("申请编号", context.get("request_id") or "-"),
                ("说明", context.get("reason") or "未提供"),
            ),
            action_url=url,
            action_label="查看申请",
        )
    if event_type == EVENT_ORGANIZER_REVIEW_REQUIRED:
        return _render_email_html(
            eyebrow="OpenList 整理待审核",
            heading=title,
            summary="Organizer 已生成整理计划，但存在需要管理员确认的识别或目标路径问题。",
            tone="warning",
            rows=(
                ("Organizer 任务", context.get("task_id") or "-"),
                ("入库任务", context.get("job_id") or "-"),
                ("异常项目", context.get("issue_count", 0)),
                ("原因", context.get("reason") or "需要人工确认"),
            ),
            action_url=url,
            action_label="进入 Organizer 审核",
        )
    if event_type == EVENT_JOB_FAILED:
        return _render_email_html(
            eyebrow="入库任务失败",
            heading=title,
            summary="任务执行未完成，请进入后台查看日志并处理失败原因。",
            tone="danger",
            rows=(
                ("任务编号", context.get("job_id") or "-"),
                ("失败阶段", context.get("stage") or "-"),
                ("错误信息", _clip(str(context.get("error") or "未提供"), 300)),
            ),
            action_url=url,
            action_label="查看任务详情",
        )
    if event_type == EVENT_JOB_DONE:
        return _render_email_html(
            eyebrow="入库任务完成",
            heading=title,
            summary="资源已经完成入库处理，可以在媒体库中继续查看。",
            tone="success",
            rows=(("任务编号", context.get("job_id") or "-"),),
            action_url=url,
            action_label="打开管理后台",
        )
    if event_type == EVENT_DIGEST_DAILY:
        return _render_email_html(
            eyebrow="每日汇总",
            heading="过去 24 小时入库概览",
            summary="以下是系统最近一天的申请和任务处理情况。",
            tone="info",
            rows=(
                ("新申请", context.get("new_count", 0)),
                ("待审核", context.get("pending_review_count", 0)),
                ("已完成", context.get("done_count", 0)),
                ("失败", context.get("failed_count", 0)),
            ),
            action_url=url,
            action_label="查看完整数据",
        )
    return _render_email_html(
        eyebrow="系统通知",
        heading="影视搜索状态更新",
        summary=str(context.get("message") or "系统产生了一条新的通知。"),
        tone="info",
        action_url=url,
        action_label="打开管理后台",
    )


def build_guest_email(
    event_type: str,
    context: dict[str, Any],
    public_base_url: str,
    *,
    unsubscribe_token: str = "",
) -> tuple[str, str]:
    base = str(public_base_url or "").strip().rstrip("/")
    request_token = str(context.get("request_token") or "").strip()
    status_url = f"{base}/request/{request_token}" if base and request_token else ""
    unsubscribe_url = (
        f"{base}/api/public/notifications/unsubscribe/{unsubscribe_token}"
        if base and unsubscribe_token
        else ""
    )
    footer = (f"\n查看进度：{status_url}" if status_url else "") + (
        f"\n停止接收：{unsubscribe_url}" if unsubscribe_url else ""
    )
    title = str(context.get("title") or "未命名资源")
    if event_type == EVENT_GUEST_EMAIL_VERIFY:
        token = str(context.get("verification_token") or "")
        verify_url = f"{base}/api/public/notifications/verify/{token}" if base and token else ""
        return (
            "【影视搜索】确认接收申请状态通知",
            f"请验证邮箱以接收「{title}」的状态变化。\n验证链接：{verify_url}\n若非本人操作，请忽略此邮件。",
        )
    if event_type == EVENT_GUEST_APPROVED:
        return f"【影视搜索】申请已通过：{title}", f"申请「{title}」已通过审核，正在处理。{footer}"
    if event_type == EVENT_GUEST_REJECTED:
        return (
            f"【影视搜索】申请未通过：{title}",
            f"申请「{title}」未通过审核。\n说明：{context.get('reason') or '未提供'}{footer}",
        )
    if event_type == EVENT_JOB_DONE:
        return f"【影视搜索】资源处理完成：{title}", f"申请「{title}」已经处理完成。{footer}"
    if event_type == EVENT_JOB_FAILED:
        return (
            f"【影视搜索】资源处理失败：{title}",
            f"申请「{title}」处理失败，请稍后查看状态或联系管理员。{footer}",
        )
    return "【影视搜索】申请状态更新", f"申请「{title}」状态已更新。{footer}"


def build_guest_email_html(
    event_type: str,
    context: dict[str, Any],
    public_base_url: str,
    *,
    unsubscribe_token: str = "",
) -> str:
    """构建访客 HTML 邮件，不暴露后台地址。"""
    base = str(public_base_url or "").strip().rstrip("/")
    request_token = str(context.get("request_token") or "").strip()
    status_url = f"{base}/request/{request_token}" if base and request_token else ""
    unsubscribe_url = (
        f"{base}/api/public/notifications/unsubscribe/{unsubscribe_token}"
        if base and unsubscribe_token
        else ""
    )
    title = str(context.get("title") or "未命名资源")
    secondary_links = (("停止接收此申请的邮件", unsubscribe_url),) if unsubscribe_url else ()
    if event_type == EVENT_GUEST_EMAIL_VERIFY:
        token = str(context.get("verification_token") or "")
        verify_url = f"{base}/api/public/notifications/verify/{token}" if base and token else ""
        return _render_email_html(
            eyebrow="确认邮箱",
            heading=title,
            summary="请确认这个邮箱由你本人填写。验证后，你会收到该申请的状态变化。",
            tone="info",
            action_url=verify_url,
            action_label="验证并接收通知",
            footer="若非本人操作，可直接忽略这封邮件。",
        )
    if event_type == EVENT_GUEST_APPROVED:
        return _render_email_html(
            eyebrow="申请已通过",
            heading=title,
            summary="你的申请已通过审核，系统正在继续处理资源。",
            tone="success",
            action_url=status_url,
            action_label="查看处理进度",
            secondary_links=secondary_links,
        )
    if event_type == EVENT_GUEST_REJECTED:
        return _render_email_html(
            eyebrow="申请未通过",
            heading=title,
            summary="你的申请未通过审核。",
            tone="danger",
            rows=(("说明", context.get("reason") or "未提供"),),
            action_url=status_url,
            action_label="查看申请状态",
            secondary_links=secondary_links,
        )
    if event_type == EVENT_JOB_DONE:
        return _render_email_html(
            eyebrow="资源处理完成",
            heading=title,
            summary="你提交的资源已经处理完成。",
            tone="success",
            action_url=status_url,
            action_label="查看最终状态",
            secondary_links=secondary_links,
        )
    if event_type == EVENT_JOB_FAILED:
        return _render_email_html(
            eyebrow="资源处理失败",
            heading=title,
            summary="这次处理未能完成，请稍后查看状态或联系管理员。",
            tone="danger",
            action_url=status_url,
            action_label="查看申请状态",
            secondary_links=secondary_links,
        )
    return _render_email_html(
        eyebrow="申请状态更新",
        heading=title,
        summary="你提交的申请状态已经更新。",
        tone="info",
        action_url=status_url,
        action_label="查看申请状态",
        secondary_links=secondary_links,
    )


def _render_email_html(
    *,
    eyebrow: str,
    heading: str,
    summary: str,
    tone: str,
    rows: tuple[tuple[str, Any], ...] = (),
    action_url: str = "",
    action_label: str = "查看详情",
    footer: str = "此邮件由影视搜索通知中心自动发送，请勿直接回复。",
    secondary_links: tuple[tuple[str, str], ...] = (),
) -> str:
    tones = {
        "success": ("#137a4a", "#e8f7ef", "#b7e5cc"),
        "warning": ("#9a5b00", "#fff6df", "#f0d493"),
        "danger": ("#b4233a", "#fff0f2", "#f0bbc4"),
        "info": ("#1769aa", "#edf6ff", "#bfdcf4"),
    }
    accent, tint, border = tones.get(tone, tones["info"])
    row_html = "".join(
        '<tr><td style="padding:10px 0;color:#667085;font-size:13px;vertical-align:top;width:96px;">'
        f"{html.escape(str(label))}</td>"
        '<td style="padding:10px 0;color:#172033;font-size:14px;font-weight:600;vertical-align:top;word-break:break-word;">'
        f"{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    details_html = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="margin-top:22px;border-collapse:collapse;border-top:1px solid #e7ebf1;">'
        f"{row_html}</table>"
        if row_html
        else ""
    )
    safe_action_url = html.escape(str(action_url or ""), quote=True)
    action_html = (
        '<table role="presentation" cellspacing="0" cellpadding="0" style="margin-top:24px;"><tr><td '
        f'style="border-radius:6px;background:{accent};"><a href="{safe_action_url}" '
        'style="display:inline-block;padding:12px 20px;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;">'
        f"{html.escape(str(action_label))}</a></td></tr></table>"
        if safe_action_url
        else ""
    )
    links_html = "".join(
        f'<a href="{html.escape(str(url), quote=True)}" style="color:#667085;text-decoration:underline;">{html.escape(str(label))}</a>'
        for label, url in secondary_links
        if url
    )
    if links_html:
        links_html = f'<div style="margin-top:10px;font-size:12px;">{links_html}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f5f8;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f5f8;"><tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;max-width:620px;background:#ffffff;border:1px solid #e2e7ee;border-radius:8px;overflow:hidden;">
<tr><td style="padding:18px 24px;background:#172033;color:#ffffff;font-size:15px;font-weight:700;">影视搜索 <span style="color:#9aa8bc;font-size:12px;font-weight:500;">通知中心</span></td></tr>
<tr><td style="padding:28px 24px 30px;">
<div style="display:inline-block;padding:5px 9px;border:1px solid {border};border-radius:4px;background:{tint};color:{accent};font-size:12px;font-weight:700;">{html.escape(str(eyebrow))}</div>
<h1 style="margin:16px 0 10px;color:#172033;font-size:24px;line-height:1.4;letter-spacing:0;word-break:break-word;">{html.escape(str(heading))}</h1>
<p style="margin:0;color:#536074;font-size:15px;line-height:1.8;word-break:break-word;">{html.escape(str(summary))}</p>
{details_html}{action_html}
</td></tr>
<tr><td style="padding:18px 24px;border-top:1px solid #e7ebf1;background:#fafbfc;color:#7a8495;font-size:12px;line-height:1.7;">{html.escape(str(footer))}{links_html}</td></tr>
</table></td></tr></table></body></html>"""


def build_webhook_context(event_type: str, context: dict[str, Any], public_base_url: str = "") -> dict[str, Any]:
    """给 Webhook 的统一结构体提供 subject / message / admin_url。"""
    safe_context = sanitize_context(event_type, context)
    subject, body = build_email(event_type, safe_context, public_base_url)
    return {
        "subject": subject,
        "message": body,
        "admin_url": _admin_url(public_base_url, context),
        "context": safe_context,
    }


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"
