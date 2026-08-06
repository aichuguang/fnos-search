"""DestinationResolver：按管理员规则解析来源的目标与账号。

规则是目标选择的唯一真源。客户端只能提交 ``destination_id`` 或完全不提交；
任何其他基础设施字段（账号、mount、路径、remote）都被忽略。目标不存在、
已禁用、受众不允许、或磁链指向非六盘时返回 ``None`` 阻止提交（§15.1、§15.3）。
"""

from __future__ import annotations

from ..domain.models import DestinationDescriptor, SourceDescriptor
from . import (
    AccountsService,
    DestinationAccountUnavailable,
    MagnetTargetRestricted,
    RulesService,
    SourceAccountUnavailable,
    TargetDisabled,
    TargetNotSelectable,
    TargetNotFound,
    TargetService,
)


class DestinationResolver:
    def __init__(
        self,
        rules_service: RulesService,
        target_service: TargetService,
        accounts_service: AccountsService,
    ):
        self.rules_service = rules_service
        self.target_service = target_service
        self.accounts_service = accounts_service

    def resolve(
        self,
        source: SourceDescriptor,
        category_key: str,
        audience: str = "guest",
    ) -> DestinationDescriptor | None:
        """按规则解析管理员分配的默认目标；无可用目标返回 ``None`` 阻止提交。"""
        destination_id = self.rules_service.resolve_default(
            source_provider_kind=source.provider_kind,
            source_form=source.source_form,
            category_key=category_key,
            audience=audience,
        )
        if not destination_id:
            return None
        target = self.target_service.get(destination_id)
        return self._descriptor_for_target(source, target, audience)

    def resolve_requested(
        self,
        source: SourceDescriptor,
        category_key: str,
        destination_id: str,
        audience: str = "guest",
    ) -> DestinationDescriptor:
        """客户端显式选择的目标：必须存在、启用、受众允许、规则允许。

        任何伪造或非法目标都抛错（§14.5 TARGET_NOT_SELECTABLE /
        MAGNET_TARGET_RESTRICTED），绝不回落到其它盘。
        """
        target = self.target_service.get(destination_id)
        if target is None:
            raise TargetNotFound(f"保存位置不存在：{destination_id}")
        if not target.enabled:
            raise TargetDisabled(f"保存位置已停用：{destination_id}")
        if audience == "guest" and target.audience != "guest":
            raise TargetNotSelectable(f"访客不能选择该保存位置：{destination_id}")
        if source.source_form == "magnet" and target.provider_kind != "sixpan":
            raise MagnetTargetRestricted("磁链目前只能保存到六盘")
        if not self.rules_service.is_allowed(
            source_provider_kind=source.provider_kind,
            source_form=source.source_form,
            category_key=category_key,
            destination_id=destination_id,
            audience=audience,
        ):
            raise TargetNotSelectable(f"规则不允许该保存位置：{destination_id}")
        return DestinationDescriptor(
            destination_id=target.destination_id,
            provider_kind=target.provider_kind,
            account_scope=target.account_scope,
            target_revision=target.current_revision,
        )

    def _descriptor_for_target(
        self,
        source: SourceDescriptor,
        target: object | None,
        audience: str,
    ) -> DestinationDescriptor | None:
        if target is None:
            return None
        if not target.enabled:
            return None
        if audience == "guest" and target.audience != "guest":
            return None
        if source.source_form == "magnet" and target.provider_kind != "sixpan":
            return None
        # 目标 Provider 需要账号作用域时，必须存在激活账号，否则当前资源不可保存。
        if target.account_scope:
            selection = self.accounts_service.get_selection(target.provider_kind, target.account_scope)
            if selection is None:
                return None
        return DestinationDescriptor(
            destination_id=target.destination_id,
            provider_kind=target.provider_kind,
            account_scope=target.account_scope,
            target_revision=target.current_revision,
        )

    def resolve_account(
        self,
        provider_kind: str,
        scope: str = "default",
        *,
        role: str = "destination",
    ) -> tuple[str, int, int]:
        """从当前激活账号选择解析 ``(account_id, account_revision, selection_revision)``。

        ``role`` 为 ``source`` 或 ``destination``，决定失败时抛出的错误类别
        （SOURCE_ACCOUNT_UNAVAILABLE / DESTINATION_ACCOUNT_UNAVAILABLE）。
        """
        selection = self.accounts_service.get_selection(provider_kind, scope)
        error_type = SourceAccountUnavailable if role == "source" else DestinationAccountUnavailable
        if selection is None:
            raise error_type(f"{provider_kind} 当前没有激活账号（scope={scope}）")
        account = self.accounts_service.get_account(selection.account_id)
        if account is None or not account.enabled:
            raise error_type(f"{provider_kind} 账号 {selection.account_id} 不可用")
        if not self.accounts_service.has_revision(selection.account_id, selection.account_revision):
            raise error_type(f"{provider_kind} 账号修订 {selection.account_revision} 不存在")
        return (selection.account_id, selection.account_revision, selection.selection_revision)
