"""DestinationResolver：按管理员规则解析来源的目标与账号。

规则是目标选择的唯一真源。客户端不得提交 ``destination_id``；
任何基础设施字段（账号、mount、路径、remote）都直接拒绝。目标不存在、
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
        destination_id = self.rules_service.resolve_destination(
            source.search_source_id or source.provider_kind,
            source.source_form,
            category_key,
            audience,
        )
        if not destination_id:
            return None
        target = self.target_service.get_target(destination_id)
        return self._descriptor_for_target(source, target, audience)

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
        if source.source_form == "magnet" and target.provider_kind != "sixpan":
            return None
        try:
            binding = self.target_service.resolve_binding(
                target.destination_id, target.current_revision
            )
        except Exception:
            return None
        if binding.account_id:
            account = self.accounts_service.get_account(binding.account_id)
            if (
                account is None
                or not account.enabled
                or getattr(account, "last_test_status", "") != "ok"
            ):
                return None
            if not self.accounts_service.has_revision(
                binding.account_id, binding.account_revision
            ):
                return None
        return DestinationDescriptor(
            destination_id=target.destination_id,
            provider_kind=target.provider_kind,
            target_revision=target.current_revision,
            binding_id=binding.binding_id,
            binding_revision=binding.current_revision,
            account_id=binding.account_id,
            account_revision=binding.account_revision,
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
        if getattr(account, "last_test_status", "") != "ok":
            raise error_type(f"{provider_kind} 当前账号检测未通过")
        if not self.accounts_service.has_revision(selection.account_id, selection.account_revision):
            raise error_type(f"{provider_kind} 账号修订 {selection.account_revision} 不存在")
        return (selection.account_id, selection.account_revision, selection.selection_revision)
