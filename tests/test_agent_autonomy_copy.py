from __future__ import annotations

from pathlib import Path

from fnos_media_import.agent.mcp.handlers import INSTRUCTIONS, SERVER_VERSION, McpHandler


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_protocol_guidance_supports_autonomous_execution_without_removing_guards() -> None:
    assert SERVER_VERSION == "0.3.0"
    assert "apply_organizer_task(execute=true)" in INSTRUCTIONS
    assert "retry_organizer_task(execute=true)" in INSTRUCTIONS
    assert "organize.apply" in INSTRUCTIONS
    assert "计划指纹" in INSTRUCTIONS and "身份、路径和冲突" in INSTRUCTIONS
    assert "不强制逐次人工确认" in INSTRUCTIONS
    assert "不代表只能由人类操作" in INSTRUCTIONS
    assert "默认 execute=false" in INSTRUCTIONS


def test_organizer_prompt_allows_ai_review_and_only_escalates_by_user_rules() -> None:
    handler = object.__new__(McpHandler)
    response = handler._prompts_get({"name": "organize_pending"})
    text = response["messages"][0]["content"]["text"]
    assert "apply_organizer_task(execute=true)" in text
    assert "retry_organizer_task(execute=true)" in text
    assert "rollback_organizer_run(confirm=true)" in text
    assert "waiting_review" in text and "不限制为人类操作" in text
    assert "证据不足或我的 Agent 规则要求时才询问我" in text
    assert "经我确认后" not in text and "给用户确认" not in text


def test_subscription_prompt_has_no_mandatory_human_approval_step() -> None:
    handler = object.__new__(McpHandler)
    response = handler._prompts_get({"name": "follow_series", "arguments": {"title": "Example"}})
    text = response["messages"][0]["content"]["text"]
    assert "按我的 Agent 规则判断是否需要询问" in text
    assert "给我确认" not in text and "确认后再正式创建" not in text


def test_agent_docs_keep_35_tools_and_describe_both_execution_modes() -> None:
    docs = _text("docs/mcp-agent.md")
    readme = _text("README.md")
    for text in (docs, readme):
        assert "0.3.0" in text and "35" in text
        assert "execute=true" in text and "execute=false" in text
        assert "plan_hash" in text and "organize.apply" in text
        assert "不强制" in text and "用户" in text
        assert "waiting_review" in text
    assert "把摘要给用户确认后" not in docs
    assert "两步确认" not in docs


def test_agent_ui_explains_token_authority_without_per_task_human_grant_claim() -> None:
    script = _text("static/admin-agent.js")
    template = _text("templates/admin.html")
    assert "完整（自主整理/恢复）" in script
    assert "只读" in script and "修改/仅重扫" in script
    assert "是否询问人由 Agent 规则决定" in script
    assert "full 可自主整理与恢复" in template
    assert "一次性「执行移动」权限" not in template
    assert "admin-agent.js?v=20261108-agent-autonomy-v003" in template
