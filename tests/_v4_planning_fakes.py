"""v4 planning 假依赖：供旧测试在一次性切换到 v4 后复用。

V1.0 设计文档 §21 一次性切换：旧 v3 执行器不保留。仍测试提交栅栏/取消/幂等等
v4 不变量的测试，通过本模块注入假 SourceResolver/DestinationResolver/
plan_import/validate，避免依赖真实账号/目标/规则服务。
"""

from __future__ import annotations

from typing import Any

from fnos_media_import.domain.models import DestinationDescriptor, SourceDescriptor


def fake_source(provider_kind: str = "quark", source_form: str = "share") -> SourceDescriptor:
    return SourceDescriptor(
        provider_kind=provider_kind,
        source_form=source_form,
        canonical_source_id=f"{provider_kind}:{source_form}:fake-source",
        account_scope="default",
        selected_items_hash="",
    )


def fake_destination(destination_id: str = "mobile_main", provider_kind: str = "mobile") -> DestinationDescriptor:
    return DestinationDescriptor(
        destination_id=destination_id,
        provider_kind=provider_kind,
        account_scope="default" if provider_kind != "local" else None,
        target_revision=1,
    )


def fake_source_resolver(provider_kind: str = "quark", source_form: str = "share") -> Any:
    class _Resolver:
        def resolve(self, record: dict[str, Any]) -> SourceDescriptor:
            return fake_source(provider_kind, source_form)

    return _Resolver()


def fake_destination_resolver(
    destination_id: str = "mobile_main",
    provider_kind: str = "mobile",
    *,
    none_destination: bool = False,
) -> Any:
    class _Resolver:
        def resolve(self, _source: Any, _category: str, audience: str = "admin") -> DestinationDescriptor | None:
            if none_destination:
                return None
            return fake_destination(destination_id, provider_kind)

        def resolve_account(self, provider_kind: str, scope: str, *, role: str) -> dict[str, Any] | None:
            return {
                "account_id": f"acc_{provider_kind}",
                "account_revision": 1,
                "selection_revision": 1,
            }

    return _Resolver()


def fake_plan_import(destination_id: str = "mobile_main") -> Any:
    def _plan(**kwargs: Any) -> dict[str, Any]:
        return {
            "version": 4,
            "job_id": int(kwargs.get("job_id") or 0),
            "source": {"provider_kind": "quark", "source_form": "share", "canonical_source_id": "quark:share:fake", "account_id": "acc_quark", "account_revision": 1, "account_selection_revision": 1},
            "destination": {"id": destination_id, "provider_kind": "mobile", "target_revision": 1, "account_id": "acc_mobile", "account_revision": 1, "account_selection_revision": 1},
            "transfer": {"strategy": "relay_upload_mobile", "executor": "mobile_relay", "idempotency_key": "import:fake", "delete_source_after_ack": True},
            "bindings": {},
            "paths": {
                "source_landing": {"namespace_id": "openlist:c:sm", "path": "/_fnos_ingress/job-1"},
                "destination_staging": {"namespace_id": "openlist:c:dm", "path": "/_fnos_staging/movie/job-1"},
                "destination_final": {"namespace_id": "openlist:c:dm", "path": "/电影"},
                "organizer": None,
                "local_temp": None,
            },
            "verification": {"minimum": ["file_count", "total_size"], "checksum": "when_supported"},
            "postprocess": {"organizer_enabled": False, "media_refresh_enabled": False},
        }

    return _plan


def fake_validate_v4_plan() -> Any:
    def _validate(plan: dict[str, Any], _job_record: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return plan

    return _validate
