"""移动云盘（CMCC API）目标驱动。

CMCC 使用自定义 API 上传（sha256 秒传 + 分片 PUT），走 relay_upload 策略。
"""

from __future__ import annotations

from typing import Any

from .._pathutil import is_unsafe_path, normalize_path, normalize_visible_path, staging_path_for
from ..types import (
    DestinationPathPlan,
    PathRef,
    StorageCapabilities,
    StorageDriver,
    StorageTarget,
    WorkerSpec,
)


class CmccApiDriver:
    kind = "cmcc_api"

    def validate(self, target: StorageTarget) -> list[str]:
        errors: list[str] = []
        config = target.config
        if not str(config.get("host") or "").strip():
            errors.append(f"目标 {target.destination_id} 缺少 host")
        auth_mode = str(config.get("auth_mode") or "web_basic").strip().lower()
        if auth_mode == "web_basic" and not str(config.get("access_token") or "").strip():
            errors.append(f"目标 {target.destination_id} 的 web_basic 认证缺少 access_token")
        if is_unsafe_path(config.get("base_path")):
            if str(config.get("base_path") or "").strip():
                errors.append(f"目标 {target.destination_id} 的 base_path 不合法")
        return errors

    def capabilities(self, target: StorageTarget) -> StorageCapabilities:
        return StorageCapabilities(
            supports_native_save=(
                str(target.config.get("native_ingress") or "").strip().lower()
                == "cloud139_direct"
            ),
            supports_relay_upload=True,
            # The current client has no quota endpoint; do not advertise a
            # capability the health check cannot verify.
            supports_quota=False,
            supports_atomic_rename=False,
        )

    def resolve_paths(
        self,
        *,
        target: StorageTarget,
        category_key: str,
        job_id: int,
        staging_enabled: bool,
        staging_dir_name: str,
    ) -> DestinationPathPlan:
        category = target.category(category_key)
        raw_final_path = str(category.final_root or "").strip().replace("\\", "/")
        if not raw_final_path or any(part in {"", ".", ".."} for part in raw_final_path.strip("/").split("/")):
            raise ValueError(f"分类 {category_key} 的 final_root 不合法")
        final_path = normalize_path(raw_final_path)
        namespace_id = str(target.config.get("namespace_id") or f"cmcc:{target.destination_id}")
        if staging_enabled:
            staging_path = staging_path_for(
                final_root=final_path,
                staging_dir_name=staging_dir_name,
                category_label=final_path.rsplit("/", 1)[-1] if final_path else category_key,
                job_id=job_id,
            )
        else:
            staging_path = final_path
        organizer = None
        organizer_ns = str(category.organizer_namespace_id or "").strip()
        organizer_root = normalize_visible_path(category.organizer_root)
        if organizer_ns and organizer_root:
            organizer_path = organizer_root
            if staging_enabled:
                organizer_path = normalize_visible_path(
                    staging_path_for(
                        final_root=organizer_root,
                        staging_dir_name=staging_dir_name,
                        category_label=organizer_root.rsplit("/", 1)[-1] or category_key,
                        job_id=job_id,
                    )
                )
            organizer = PathRef(namespace_id=organizer_ns, path=organizer_path)
        return DestinationPathPlan(
            destination_id=target.destination_id,
            driver=self.kind,
            namespace_id=namespace_id,
            job_id=int(job_id),
            staging=PathRef(namespace_id=namespace_id, path=staging_path),
            final=PathRef(namespace_id=namespace_id, path=final_path),
            organizer=organizer,
        )

    def worker_spec(
        self,
        *,
        target: StorageTarget,
        paths: DestinationPathPlan,
        strategy: str,
        source: PathRef,
        delete_source_after_ack: bool = True,
    ) -> WorkerSpec:
        if strategy != "relay_upload":
            raise ValueError(f"cmcc_api 不支持 Worker 策略：{strategy}")
        if not source.namespace_id or not source.path:
            raise ValueError("relay_upload 缺少来源路径")
        return WorkerSpec(
            mode="relay_upload",
            source=source,
            destination=paths.staging,
            uploader="cmcc_api",
            expected_job_id=paths.job_id,
            delete_source_after_ack=bool(delete_source_after_ack),
        )
