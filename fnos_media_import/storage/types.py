"""存储目标可插拔化的领域类型（v3）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class TargetCategoryConfig:
    """目标在单个分类下的路径配置。"""

    final_root: str
    organizer_namespace_id: str = ""
    organizer_root: str = ""


@dataclass(frozen=True)
class StorageTarget:
    """管理员配置的一个目标实例。

    ``destination_id`` 是稳定业务标识；``driver`` 是实现类型。二者不得混用。
    """

    destination_id: str
    name: str
    driver: str
    enabled: bool
    guest_selectable: bool
    config: dict[str, Any] = field(default_factory=dict)
    categories: dict[str, TargetCategoryConfig] = field(default_factory=dict)

    def category(self, category_key: str) -> TargetCategoryConfig:
        return self.categories.get(category_key, TargetCategoryConfig(final_root=""))


@dataclass(frozen=True)
class StorageCapabilities:
    """目标端能力。ImportPlanner 据此选择传输策略。"""

    supports_native_save: bool = False
    supports_rclone_dest: bool = False
    supports_relay_upload: bool = False
    supports_quota: bool = False
    supports_organizer_access: bool = False
    supports_atomic_rename: bool = False
    supports_server_side_copy: bool = False
    supports_checksum: bool = False


@dataclass(frozen=True)
class PathRef:
    """一条带命名空间的路径。路径身份是 (namespace_id, normalized_path)。"""

    namespace_id: str
    path: str


@dataclass(frozen=True)
class DestinationPathPlan:
    """驱动解析出的目标端路径计划（存储命名空间 + Organizer 命名空间）。"""

    destination_id: str
    driver: str
    namespace_id: str
    job_id: int
    staging: PathRef
    final: PathRef
    organizer: PathRef | None


@dataclass(frozen=True)
class WorkerSpec:
    """Worker 执行规格。source 由 ImportPlanner 组合后填充。"""

    mode: Literal["rclone_copy", "relay_upload"]
    source: PathRef
    destination: PathRef
    uploader: str | None
    expected_job_id: int
    delete_source_after_ack: bool


class StorageDriver(Protocol):
    """目标存储驱动。只理解自己的目标配置，不接收 route/cloud139/sixpan 等供应商配置。"""

    kind: str

    def validate(self, target: StorageTarget) -> list[str]:
        """返回配置错误；空列表表示配置可用。"""
        ...

    def capabilities(self, target: StorageTarget) -> StorageCapabilities:
        ...

    def resolve_paths(
        self,
        *,
        target: StorageTarget,
        category_key: str,
        job_id: int,
        staging_enabled: bool,
        staging_dir_name: str,
    ) -> DestinationPathPlan:
        ...

    def worker_spec(
        self,
        *,
        target: StorageTarget,
        paths: DestinationPathPlan,
        strategy: str,
        source: PathRef,
        delete_source_after_ack: bool = True,
    ) -> WorkerSpec:
        ...
