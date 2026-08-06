"""rclone 通用目标驱动。

目标可以是任意已配置 rclone remote 或受控本地绝对路径，走 rclone_copy 策略。
"""

from __future__ import annotations

import re
from typing import Any

from .._pathutil import is_unsafe_path, normalize_path, normalize_visible_path, resolve_local_target, staging_path_for
from ..types import (
    DestinationPathPlan,
    PathRef,
    StorageCapabilities,
    StorageDriver,
    StorageTarget,
    WorkerSpec,
)


class RcloneDriver:
    kind = "rclone"

    _REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def validate(self, target: StorageTarget) -> list[str]:
        errors: list[str] = []
        config = target.config
        remote = str(config.get("remote") or "").strip()
        root = str(config.get("root") or "").strip()
        if bool(remote) == bool(root):
            errors.append(f"目标 {target.destination_id} 必须配置 remote 或本地 root")
        if remote and not self._REMOTE_RE.fullmatch(remote):
            errors.append(f"目标 {target.destination_id} 的 remote 不合法")
        if root:
            try:
                resolve_local_target(root, "_probe")
            except ValueError as exc:
                errors.append(f"目标 {target.destination_id} 的 root 不合法：{exc}")
        namespace = str(config.get("namespace_id") or "").strip()
        if remote and namespace and namespace != f"rclone:{remote}":
            errors.append(f"目标 {target.destination_id} 的 namespace_id 必须与 remote 一致")
        if root and namespace and namespace != f"local:{target.destination_id}":
            errors.append(f"目标 {target.destination_id} 的本地 namespace_id 不合法")
        return errors

    def capabilities(self, target: StorageTarget) -> StorageCapabilities:
        config = target.config
        organizer_ns = str(config.get("organizer_namespace_id") or "").strip()
        return StorageCapabilities(
            supports_rclone_dest=True,
            supports_organizer_access=bool(organizer_ns),
            supports_atomic_rename=False,
            supports_checksum=True,
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
        config = target.config
        remote = str(config.get("remote") or "").strip()
        root = str(config.get("root") or "").strip()
        raw_final_path = str(category.final_root or "").strip().replace("\\", "/")
        if not raw_final_path or any(part in {"", ".", ".."} for part in raw_final_path.strip("/").split("/")):
            raise ValueError(f"分类 {category_key} 的 final_root 不合法")
        final_path = normalize_path(raw_final_path)
        if remote:
            namespace_id = f"rclone:{remote}"
            final_ref = PathRef(namespace_id=namespace_id, path=final_path)
            if staging_enabled:
                staging_path = staging_path_for(
                    final_root=final_path,
                    staging_dir_name=staging_dir_name,
                    category_label=final_path.rsplit("/", 1)[-1] if final_path else category_key,
                    job_id=job_id,
                )
            else:
                staging_path = final_path
            staging_ref = PathRef(namespace_id=namespace_id, path=staging_path)
        else:
            namespace_id = f"local:{target.destination_id}"
            final_ref = PathRef(namespace_id=namespace_id, path=resolve_local_target(root, final_path))
            if staging_enabled:
                staging_path = staging_path_for(
                    final_root=final_path,
                    staging_dir_name=staging_dir_name,
                    category_label=final_path.rsplit("/", 1)[-1] if final_path else category_key,
                    job_id=job_id,
                )
                staging_ref = PathRef(namespace_id=namespace_id, path=resolve_local_target(root, staging_path))
            else:
                staging_ref = final_ref
        organizer = None
        organizer_ns = str(category.organizer_namespace_id or config.get("organizer_namespace_id") or "").strip()
        organizer_root = normalize_visible_path(category.organizer_root or config.get("organizer_root"))
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
            staging=staging_ref,
            final=final_ref,
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
        if strategy != "rclone_copy":
            raise ValueError(f"rclone 不支持 Worker 策略：{strategy}")
        if not source.namespace_id or not source.path:
            raise ValueError("rclone_copy 缺少来源路径")
        return WorkerSpec(
            mode="rclone_copy",
            source=source,
            destination=paths.staging,
            uploader=None,
            expected_job_id=paths.job_id,
            delete_source_after_ack=bool(delete_source_after_ack),
        )
