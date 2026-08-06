"""来源到目标规则服务：默认目标分配、磁链约束与解析。

§9 说明了一个全局默认盘为什么不够：磁链只能去六盘、移动分享到移动适合原生
转存等。因此默认目标必须与「来源 + 来源形态 + 分类 + 受众」关联，并通过
``source_destination_rules`` 持久化。

规则语义：

- ``category_key='*'`` 表示 Provider 级规则；精确分类规则优先于 ``*``。
- 同一 (source_provider_kind, source_form, category_key, audience) 最多一个默认目标。
  规则本身通过 ``guest_allowed`` / ``admin_allowed`` 同时表达两个受众的可见性。
- ``source_form == magnet`` 只允许映射到 ``provider_kind == sixpan`` 的目标
  （§10.6），V1.0 不开放磁链到其他目标。
- ``resolve_destination`` 是服务端分配（访客从不自己选目标），返回管理员指定
  的默认目标或 ``None``；无目标时上层必须阻止提交，绝不回退到其他盘。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..config import CATEGORY_KEYS
from ..domain.models import SourceDestinationRule
from .target_service import TargetNotFoundError

_ALLOWED_SOURCE_FORMS = frozenset({"share", "magnet", "local", "http"})


class RuleError(Exception):
    """入库规则领域错误基类。"""


class RuleValidationError(RuleError, ValueError):
    """规则不满足约束（磁链限制 / 默认唯一 / 字段缺失）。"""


class RuleNotFoundError(RuleError):
    """规则不存在。"""


def rule_to_dict(rule: SourceDestinationRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "source_provider_kind": rule.source_provider_kind,
        "source_form": rule.source_form,
        "category_key": rule.category_key,
        "destination_id": rule.destination_id,
        "guest_allowed": rule.guest_allowed,
        "admin_allowed": rule.admin_allowed,
        "is_default": rule.is_default,
        "priority": rule.priority,
        "enabled": rule.enabled,
    }


class DestinationRulesService:
    """来源到目标规则 CRUD、默认唯一约束与服务端目标解析。"""

    def __init__(self, database: Any, target_service: Any):
        self.database = database
        self.target_service = target_service

    # ------------------------------------------------------------- CRUD

    def list_rules(self) -> list[SourceDestinationRule]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM source_destination_rules "
                "ORDER BY source_provider_kind, source_form, category_key, priority, rule_id"
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def get_rule(self, rule_id: str) -> SourceDestinationRule | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_destination_rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_rule(row)

    def create_rule(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        destination_id: str,
        guest_allowed: bool = False,
        admin_allowed: bool = True,
        is_default: bool = False,
        priority: int = 100,
        enabled: bool = True,
        rule_id: str | None = None,
    ) -> SourceDestinationRule:
        rule = SourceDestinationRule(
            rule_id=str(rule_id or "").strip() or f"rule_{uuid.uuid4().hex[:10]}",
            source_provider_kind=str(source_provider_kind or "").strip(),
            source_form=str(source_form or "").strip(),
            category_key=str(category_key or "").strip(),
            destination_id=str(destination_id or "").strip(),
            guest_allowed=bool(guest_allowed),
            admin_allowed=bool(admin_allowed),
            is_default=bool(is_default),
            priority=max(0, int(priority or 100)),
            enabled=bool(enabled),
        )
        self._check_rule_valid(rule)
        with self.database.connect() as conn:
            self._assert_default_unique(conn, rule=rule)
            conn.execute(
                "INSERT INTO source_destination_rules "
                "(rule_id, source_provider_kind, source_form, category_key, destination_id, "
                " guest_allowed, admin_allowed, is_default, priority, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.rule_id,
                    rule.source_provider_kind,
                    rule.source_form,
                    rule.category_key,
                    rule.destination_id,
                    1 if rule.guest_allowed else 0,
                    1 if rule.admin_allowed else 0,
                    1 if rule.is_default else 0,
                    rule.priority,
                    1 if rule.enabled else 0,
                ),
            )
        created = self.get_rule(rule.rule_id)
        if created is None:  # pragma: no cover - 防御
            raise RuleValidationError(f"创建失败：{rule.rule_id}")
        return created

    def update_rule(self, rule_id: str, **updates: Any) -> SourceDestinationRule:
        existing = self.get_rule(rule_id)
        if existing is None:
            raise RuleNotFoundError(f"规则不存在：{rule_id}")
        new = SourceDestinationRule(
            rule_id=existing.rule_id,
            source_provider_kind=str(updates.get("source_provider_kind") or existing.source_provider_kind),
            source_form=str(updates.get("source_form") or existing.source_form),
            category_key=str(updates.get("category_key") or existing.category_key),
            destination_id=str(updates.get("destination_id") or existing.destination_id),
            guest_allowed=bool(updates.get("guest_allowed", existing.guest_allowed)),
            admin_allowed=bool(updates.get("admin_allowed", existing.admin_allowed)),
            is_default=bool(updates.get("is_default", existing.is_default)),
            priority=max(0, int(updates.get("priority", existing.priority) or existing.priority)),
            enabled=bool(updates.get("enabled", existing.enabled)),
        )
        self._check_rule_valid(new)
        with self.database.connect() as conn:
            self._assert_default_unique(conn, rule=new, exclude_rule_id=rule_id)
            conn.execute(
                "UPDATE source_destination_rules SET "
                "source_provider_kind = ?, source_form = ?, category_key = ?, destination_id = ?, "
                "guest_allowed = ?, admin_allowed = ?, is_default = ?, priority = ?, enabled = ? "
                "WHERE rule_id = ?",
                (
                    new.source_provider_kind,
                    new.source_form,
                    new.category_key,
                    new.destination_id,
                    1 if new.guest_allowed else 0,
                    1 if new.admin_allowed else 0,
                    1 if new.is_default else 0,
                    new.priority,
                    1 if new.enabled else 0,
                    rule_id,
                ),
            )
        updated = self.get_rule(rule_id)
        if updated is None:  # pragma: no cover - 防御
            raise RuleNotFoundError(f"规则不存在：{rule_id}")
        return updated

    def delete_rule(self, rule_id: str) -> bool:
        with self.database.connect() as conn:
            cursor = conn.execute("DELETE FROM source_destination_rules WHERE rule_id = ?", (rule_id,))
        return cursor.rowcount > 0

    def put_rules(self, rules: list[dict[str, Any]]) -> list[SourceDestinationRule]:
        """PUT 全量替换：事务内校验全部规则后清空重写。"""
        parsed = [self._parse_rule(raw) for raw in rules]
        for rule in parsed:
            self._check_rule_valid(rule)
        self._assert_default_unique_set(parsed)
        with self.database.connect() as conn:
            conn.execute("DELETE FROM source_destination_rules")
            for rule in parsed:
                conn.execute(
                    "INSERT INTO source_destination_rules "
                    "(rule_id, source_provider_kind, source_form, category_key, destination_id, "
                    " guest_allowed, admin_allowed, is_default, priority, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rule.rule_id,
                        rule.source_provider_kind,
                        rule.source_form,
                        rule.category_key,
                        rule.destination_id,
                        1 if rule.guest_allowed else 0,
                        1 if rule.admin_allowed else 0,
                        1 if rule.is_default else 0,
                        rule.priority,
                        1 if rule.enabled else 0,
                    ),
                )
        return self.list_rules()

    # ------------------------------------------------------------- 解析

    def resolve_destination(
        self,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        audience: str = "guest",
    ) -> str | None:
        """服务端解析该来源 + 分类 + 受众的管理员指定默认目标；无则返回 None。

        精确分类规则优先于 ``category_key='*'``：先看精确分类默认目标，
        没有再回退到 Provider 级默认目标。只考虑 enabled=1 的规则。
        """
        audience = audience or "guest"
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM source_destination_rules "
                "WHERE source_provider_kind = ? AND source_form = ? AND enabled = 1",
                (source_provider_kind, source_form),
            ).fetchall()
        rules = [self._row_to_rule(row) for row in rows]
        if audience == "guest":
            rules = [r for r in rules if r.guest_allowed]
        else:
            rules = [r for r in rules if r.admin_allowed]
        for rule in rules:
            if rule.category_key == category_key and rule.is_default:
                return rule.destination_id
        for rule in rules:
            if rule.category_key == "*" and rule.is_default:
                return rule.destination_id
        return None

    def transfer_matrix(self) -> dict[str, Any]:
        """后台链路诊断矩阵（§17.5）：来源 x 分类默认目标 + 访客可选目标。"""
        rules = self.list_rules()
        category_keys = list(CATEGORY_KEYS)
        source_keys = sorted({(r.source_provider_kind, r.source_form) for r in rules})
        rows: list[dict[str, Any]] = []
        for provider_kind, source_form in source_keys:
            source_rules = [
                r
                for r in rules
                if r.source_provider_kind == provider_kind and r.source_form == source_form
            ]
            guest_rules = [r for r in source_rules if r.guest_allowed and r.enabled]
            rows.append(
                {
                    "source_provider_kind": provider_kind,
                    "source_form": source_form,
                    "rule_count": len(source_rules),
                    "guest_destinations": sorted({r.destination_id for r in guest_rules}),
                    "defaults": {
                        key: self.resolve_destination(provider_kind, source_form, key, "guest")
                        for key in category_keys
                    },
                }
            )
        return {
            "success": True,
            "categories": category_keys,
            "rows": rows,
        }

    # ------------------------------------------------------------- 内部实现

    def _parse_rule(self, payload: Any) -> SourceDestinationRule:
        payload = payload or {}
        return SourceDestinationRule(
            rule_id=str(payload.get("rule_id") or "").strip() or f"rule_{uuid.uuid4().hex[:10]}",
            source_provider_kind=str(payload.get("source_provider_kind") or "").strip(),
            source_form=str(payload.get("source_form") or "").strip(),
            category_key=str(payload.get("category_key") or "").strip(),
            destination_id=str(payload.get("destination_id") or "").strip(),
            guest_allowed=bool(payload.get("guest_allowed", False)),
            admin_allowed=bool(payload.get("admin_allowed", True)),
            is_default=bool(payload.get("is_default", False)),
            priority=max(0, int(payload.get("priority", 100) or 100)),
            enabled=bool(payload.get("enabled", True)),
        )

    def _check_rule_valid(self, rule: SourceDestinationRule) -> None:
        if not rule.source_provider_kind:
            raise RuleValidationError("source_provider_kind 不能为空")
        if rule.source_form not in _ALLOWED_SOURCE_FORMS:
            raise RuleValidationError(
                f"source_form 必须是 {'/'.join(sorted(_ALLOWED_SOURCE_FORMS))}"
            )
        if not rule.category_key:
            raise RuleValidationError("category_key 不能为空")
        if not rule.destination_id:
            raise RuleValidationError("destination_id 不能为空")
        target = self.target_service.get_target(rule.destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{rule.destination_id}")
        if rule.source_form == "magnet" and target.provider_kind != "sixpan":
            raise RuleValidationError("磁链只允许保存到六盘目标")

    def _assert_default_unique(
        self,
        conn: Any,
        *,
        rule: SourceDestinationRule,
        exclude_rule_id: str | None = None,
    ) -> None:
        if not rule.is_default:
            return
        rows = conn.execute(
            "SELECT rule_id, guest_allowed, admin_allowed FROM source_destination_rules "
            "WHERE source_provider_kind = ? AND source_form = ? AND category_key = ? AND is_default = 1",
            (rule.source_provider_kind, rule.source_form, rule.category_key),
        ).fetchall()
        for row in rows:
            if row["rule_id"] == exclude_rule_id:
                continue
            if rule.guest_allowed and row["guest_allowed"]:
                raise RuleValidationError(
                    f"同一来源 {rule.source_provider_kind}/{rule.source_form}/{rule.category_key} "
                    "的访客默认目标已存在"
                )
            if rule.admin_allowed and row["admin_allowed"]:
                raise RuleValidationError(
                    f"同一来源 {rule.source_provider_kind}/{rule.source_form}/{rule.category_key} "
                    "的管理员默认目标已存在"
                )

    def _assert_default_unique_set(self, rules: list[SourceDestinationRule]) -> None:
        seen_guest: set[tuple[str, str, str]] = set()
        seen_admin: set[tuple[str, str, str]] = set()
        for rule in rules:
            if not rule.is_default:
                continue
            key = (rule.source_provider_kind, rule.source_form, rule.category_key)
            if rule.guest_allowed:
                if key in seen_guest:
                    raise RuleValidationError(
                        f"同一来源 {key[0]}/{key[1]}/{key[2]} 的访客默认目标不能重复"
                    )
                seen_guest.add(key)
            if rule.admin_allowed:
                if key in seen_admin:
                    raise RuleValidationError(
                        f"同一来源 {key[0]}/{key[1]}/{key[2]} 的管理员默认目标不能重复"
                    )
                seen_admin.add(key)

    def _row_to_rule(self, row: Any) -> SourceDestinationRule:
        return SourceDestinationRule(
            rule_id=row["rule_id"],
            source_provider_kind=row["source_provider_kind"],
            source_form=row["source_form"],
            category_key=row["category_key"],
            destination_id=row["destination_id"],
            guest_allowed=bool(row["guest_allowed"]),
            admin_allowed=bool(row["admin_allowed"]),
            is_default=bool(row["is_default"]),
            priority=int(row["priority"] or 100),
            enabled=bool(row["enabled"]),
        )
