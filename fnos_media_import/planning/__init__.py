"""V1.0 多链路 Import Planner。

系统只创建、校验并执行 schema version 1 计划。生成器与
校验器必须执行相同的安全约束；任何字段缺失、修订被非法删除或身份不一致都
失败关闭，绝不根据当前后台配置“补全”计划，也绝不把旧 v2/v3 计划当作可执行计划。

模块布局：

- ``source_resolver``    从服务端可信资源记录解析来源（不信任客户端 provider 字段）。
- ``destination_resolver`` 按管理员规则解析来源的目标与账号。
- ``planner``            冻结唯一执行策略与任务级路径，返回不可变 V1 计划。
- ``plan_validator``     按 §12.4 校验冻结计划与任务身份一致，失败关闭。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..domain.models import (
    EXECUTION_PLAN_VERSION,  # noqa: F401  # re-export：本包统一使用 V1
    AccountSelection,
    OpenListMount,
    ProviderAccount,
    StorageTarget,
)
from ..providers.base import ProviderAdapter

# ---- 传输策略常量（§3.3 / §12.3） ------------------------------------------

STRATEGY_NATIVE_SAVE = "native_save"
STRATEGY_NATIVE_OFFLINE = "native_offline"
STRATEGY_RCLONE_COPY = "rclone_copy"
STRATEGY_RELAY_UPLOAD_PROVIDER = "relay_upload_provider"

RELAY_UPLOAD_STRATEGIES = frozenset({STRATEGY_RELAY_UPLOAD_PROVIDER})
DATA_PLANE_STRATEGIES = frozenset(
    {STRATEGY_RCLONE_COPY, *RELAY_UPLOAD_STRATEGIES}
)

VALID_STRATEGIES = frozenset(
    {
        STRATEGY_NATIVE_SAVE,
        STRATEGY_NATIVE_OFFLINE,
        STRATEGY_RCLONE_COPY,
        STRATEGY_RELAY_UPLOAD_PROVIDER,
    }
)

# 策略 -> 执行器（§12.3 transfer.executor）。
STRATEGY_EXECUTOR = {
    STRATEGY_NATIVE_SAVE: "native",
    STRATEGY_NATIVE_OFFLINE: "native_offline",
    STRATEGY_RCLONE_COPY: "rclone",
    STRATEGY_RELAY_UPLOAD_PROVIDER: "provider_relay",
}


def is_relay_upload_strategy(value: Any) -> bool:
    return str(value or "").strip().lower() in RELAY_UPLOAD_STRATEGIES


def is_data_plane_strategy(value: Any) -> bool:
    return str(value or "").strip().lower() in DATA_PLANE_STRATEGIES

# 部署级本地根（可被 config 的 FNOS_LOCAL_TARGET_ROOT / FNOS_RELAY_ROOT 覆盖）。
DEFAULT_LOCAL_TARGET_ROOT = "/storage-targets"
DEFAULT_RELAY_ROOT = "/data/relay"

# 任务级落地区 / 暂存区目录名（§8.5、§12.3）。
#
# 目录名与原生入库暂存统一为 <NATIVE_STAGING_DIR>（用户拍板：全局只用
# _入库暂存，不再引入 _fnos_ingress/_fnos_staging 等英文名）。两个常量的
# 语义仍不同——INGRESS_DIR 是来源侧落地区（Provider 物化 + rclone 下载），
# STAGING_DIR 是 rclone_copy 目标侧暂存区——只是目录名相同，路径形状也不同
# （落地区为 <挂载根>/_入库暂存/job-N，暂存区为 <挂载根>/_入库暂存/分类/job-N）。
# 以 UTF-8 源码字面量声明，不经过任何 shell 拼接。
INGRESS_DIR = "_入库暂存"
STAGING_DIR = "_入库暂存"

# 原生转存（native_save / native_offline）的入库暂存目录名。
#
# 原生链路没有 rclone 的挂载级暂存区：Provider 只能在自己的目录树里创建子目录。
# 暂存目录与分类目录同级（分类父目录 / <NATIVE_STAGING_DIR> / 分类名 / job-N），
# 绝不嵌在分类目录里面：媒体库（如飞牛）按分类目录扫描，暂存嵌在分类内部会把
# 搬运中的文件扫进库、入库后又失效。这里使用冻结常量而不是
# ORGANIZER_STAGING_DIR_NAME 配置值，否则管理员改配置会让已冻结的执行计划
# 校验失败。常量以 UTF-8 源码字面量声明，不经过任何 shell 拼接。
NATIVE_STAGING_DIR = "_入库暂存"

# 允许的来源形态（§3.3 SourceDescriptor.source_form）。
VALID_SOURCE_FORMS = frozenset({"share", "magnet", "local", "http"})

@dataclass(frozen=True)
class CapabilityDecision:
    """Adapter 对本次来源 + 目标 + 账号的原生能力判断。"""

    available: bool
    reason: str = ""
    code: str = ""


# ---- 统一规划错误（code 对应 §15.3 错误码） --------------------------------

class PlanningError(ValueError):
    code = "PLANNING_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or self.code)
        self.code = code or self.code


class SourceResolutionError(PlanningError):
    code = "SOURCE_UNRESOLVED"


class SourceAccountUnavailable(PlanningError):
    code = "SOURCE_ACCOUNT_UNAVAILABLE"


class DestinationAccountUnavailable(PlanningError):
    code = "DESTINATION_ACCOUNT_UNAVAILABLE"


class DestinationRequired(PlanningError):
    code = "DESTINATION_REQUIRED"


class TargetNotFound(PlanningError):
    code = "TARGET_NOT_FOUND"


class TargetDisabled(PlanningError):
    code = "TARGET_DISABLED"


class TargetNotSelectable(PlanningError):
    code = "TARGET_NOT_SELECTABLE"


class TargetIncompatible(PlanningError):
    code = "TARGET_INCOMPATIBLE"


class MagnetTargetRestricted(PlanningError):
    code = "MAGNET_TARGET_RESTRICTED"


class NativeTransferUnavailable(PlanningError):
    code = "NATIVE_TRANSFER_UNAVAILABLE"


class SourceLandingUnavailable(PlanningError):
    code = "SOURCE_LANDING_UNAVAILABLE"


class OpenlistUnavailable(PlanningError):
    code = "OPENLIST_UNAVAILABLE"


class UnsupportedPlanVersion(PlanningError):
    code = "UNSUPPORTED_PLAN_VERSION"


class PlanValidationError(PlanningError):
    code = "PLAN_VALIDATION_FAILED"


# ---- 路径与命名空间安全工具 ------------------------------------------------
#
# 路径身份是 (namespace_id, normalized_path)（§7.2）。不同 namespace 的相同
# 字符串路径不构成重叠；本地路径必须位于受控根内（§8.3）。


def normalize_path(value: Any) -> str:
    """去掉空段与 ``.`` 段，统一 ``\\`` 为 ``/``，并保证以 ``/`` 开头。"""
    text = str(value or "").strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part and part != "."]
    return "/" + "/".join(parts)


def job_dir(job_id: Any) -> str:
    return f"job-{int(job_id)}"


def path_is_safe(path: Any) -> bool:
    text = str(path or "").replace("\\", "/")
    if not text:
        return False
    if "\x00" in text or any(ord(ch) < 32 for ch in text):
        return False
    parts = text.split("/")
    if ".." in parts or "." in parts:
        return False
    return True


def paths_overlap(namespace_a: Any, path_a: Any, namespace_b: Any, path_b: Any) -> bool:
    """相同命名空间下两路径相等或互相包含即视为重叠。"""
    if str(namespace_a or "").strip() != str(namespace_b or "").strip():
        return False
    left = normalize_path(path_a).strip("/")
    right = normalize_path(path_b).strip("/")
    if not left or not right:
        return False
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def local_contained(path: Any, root: Any = DEFAULT_LOCAL_TARGET_ROOT) -> bool:
    norm = normalize_path(path)
    root_norm = normalize_path(root)
    return norm == root_norm or norm.startswith(f"{root_norm}/")


def namespace_kind(namespace_id: Any) -> str:
    text = str(namespace_id or "").strip()
    if text.startswith("openlist:"):
        return "openlist"
    if text.startswith("local:"):
        return "local"
    if text.startswith("native:"):
        return "native"
    return "unknown"


def openlist_namespace(connection_id: Any, mount_id: Any) -> str:
    return f"openlist:{connection_id}:{mount_id}"


def native_namespace(provider_kind: Any, destination_id: Any) -> str:
    return f"native:{provider_kind}:{destination_id}"


def local_namespace(destination_id: Any) -> str:
    return f"local:{destination_id}"


def split_openlist_namespace(namespace_id: Any) -> tuple[str, str] | None:
    text = str(namespace_id or "").strip()
    if not text.startswith("openlist:"):
        return None
    rest = text[len("openlist:"):]
    if ":" not in rest:
        return None
    connection_id, mount_id = rest.split(":", 1)
    if not connection_id or not mount_id:
        return None
    return connection_id, mount_id


def path_leaf(path: Any) -> str:
    return normalize_path(path).rstrip("/").rsplit("/", 1)[-1]


def mount_absolute_path(webdav_root: Any, mount_relative_path: Any) -> str:
    """把 OpenList 挂载根与挂载内相对路径拼成 OpenList 全局绝对路径。

    OpenList 的 ``/api/fs/*`` 只认全局绝对路径（``/离线下载/电视剧/...``），而分类
    定位器里保存的是挂载内相对路径（``/电视剧/...``）。rclone 侧由
    ``OpenListLocatorResolver.remote_path`` 负责拼根，Organizer 侧则由本函数负责，
    两边必须用同一套坐标系。

    ``webdav_root`` 只描述 OpenList 自己的挂载点，与 Provider 原生根（天翼云
    ``{folder_id}::{相对路径}``、夸克/移动的原生目录）是两套互不相通的坐标系，
    因此本函数只能用于 OpenList 可见路径，绝不能作用于 native 定位器值。

    幂等：已经带上挂载根前缀的路径不会被重复拼接。
    """

    root = normalize_path(webdav_root)
    relative = normalize_path(mount_relative_path)
    if root in ("", "/"):
        return relative
    if relative in ("", "/"):
        return root
    if relative == root or relative.startswith(f"{root}/"):
        return relative
    return normalize_path(f"{root}/{relative.lstrip('/')}")


def mount_webdav_root(mount: Any) -> str:
    """从冻结 mount 快照里取出 OpenList 挂载根（缺失时退化为根目录）。"""

    if not isinstance(mount, Mapping):
        return "/"
    return normalize_path(mount.get("webdav_root") or "/")


def native_staging_path(category_path: Any, job: Any) -> str:
    """由分类路径推导同级入库暂存路径（分类父目录 / _入库暂存 / 分类名 / job-N）。

    暂存目录必须位于分类目录外面：媒体库（如飞牛）按分类目录扫描，暂存嵌在
    分类内部会把搬运中的文件扫进库。返回空串表示无法派生（分类就是根目录）。
    """

    category = normalize_path(category_path)
    leaf = path_leaf(category)
    leaf_dir = str(job or "").strip().strip("/")
    if not leaf or not leaf_dir:
        return ""
    parent = category.rsplit("/", 1)[0]
    return normalize_path(f"{parent}/{NATIVE_STAGING_DIR}/{leaf}/{leaf_dir}")


def native_staging_locator(locator_kind: Any, locator_value: Any, job: Any) -> str:
    """由分类原生定位器推导任务级入库暂存定位器值。

    规划器与校验器必须用同一个推导函数，否则冻结计划的暂存路径无法复核。

    暂存目录与分类目录同级（分类父目录 / _入库暂存 / 分类名 / job-N），不嵌在
    分类目录里面，避免媒体库按分类目录扫描时看到搬运中的文件。
    ``folder_path`` 是复合定位器 ``{root_folder_id}::{相对路径}``（天翼云），只在
    相对路径一侧派生同级段；``provider_path`` / ``path`` 是纯路径定位器，取分类
    父目录派生。``folder_id`` 之类的不透明 ID 无法派生子目录，返回空串表示不支持
    原生暂存。
    """

    kind = str(locator_kind or "").strip()
    value = str(locator_value or "").strip()
    leaf_dir = str(job or "").strip().strip("/")
    if not value or not leaf_dir:
        return ""
    if kind == "folder_path":
        root_folder_id, separator, relative = value.partition("::")
        if not separator or not root_folder_id.strip() or not relative.strip():
            return ""
        staged = native_staging_path(relative, leaf_dir)
        if not staged:
            return ""
        return f"{root_folder_id}::{staged.lstrip('/')}"
    if kind in {"provider_path", "path"}:
        return native_staging_path(value, leaf_dir)
    return ""


# ---- 服务接口（Protocol） ---------------------------------------------------
#
# 本包只依赖这些最小接口。测试以 fakes 注入；真实实现（账号/目标/规则/OpenList
# 服务）在后续阶段按同一接口接入，无需修改本包。


class AdapterRegistry(Protocol):
    def get(self, provider_kind: str) -> ProviderAdapter: ...


class AccountsService(Protocol):
    def get_selection(self, provider_kind: str, scope: str = "default") -> AccountSelection | None: ...

    def get_selection_revision(
        self, provider_kind: str, scope: str, selection_revision: int
    ) -> AccountSelection | None: ...

    def get_account(self, account_id: str) -> ProviderAccount | None: ...

    def has_revision(self, account_id: str, revision: int) -> bool: ...


class TargetService(Protocol):
    def get_target(self, destination_id: str, revision: int | None = None) -> StorageTarget | None: ...

    def resolve_binding(self, destination_id: str, target_revision: int) -> Any: ...

    def get_binding(self, binding_id: str, binding_revision: int | None = None) -> Any: ...


class RulesService(Protocol):
    """来源 -> 目标规则。规则是目标选择的唯一真源。"""

    def resolve_destination(
        self,
        search_source_id: str,
        source_form: str,
        category_key: str,
        audience: str = "guest",
    ) -> str | None: ...

    def get_assignment(self, search_source_id: str, category_key: str) -> Any: ...

    def get_revision(self, assignment_id: str, revision: int) -> Any: ...


class MountService(Protocol):
    def get(self, mount_id: str) -> OpenListMount | None: ...

    def find_mount_for_account(
        self,
        provider_kind: str,
        account_id: str,
        *,
        readable: bool = True,
    ) -> OpenListMount | None: ...

    def get_revision(self, mount_id: str, revision: int) -> Any: ...


# 底部再导入子模块，避免循环导入：异常/常量先定义，子模块用 ``from . import``
# 引用它们时 __init__ 已部分初始化。
from .source_resolver import SourceResolver  # noqa: E402
from .destination_resolver import DestinationResolver  # noqa: E402
from .planner import ImportPlanner  # noqa: E402
from .plan_validator import PlanValidationDeps, PlanValidator, validate_plan  # noqa: E402
from .job_plan import (  # noqa: E402
    JobPlanError,
    job_plan_reason,
    plan_path,
    plan_strategy,
    validated_v1_execution_plan_from_job,
    v1_execution_plan_from_job,
)

__all__ = [
    "EXECUTION_PLAN_VERSION",
    "STRATEGY_NATIVE_SAVE",
    "STRATEGY_NATIVE_OFFLINE",
    "STRATEGY_RCLONE_COPY",
    "STRATEGY_RELAY_UPLOAD_PROVIDER",
    "RELAY_UPLOAD_STRATEGIES",
    "DATA_PLANE_STRATEGIES",
    "is_relay_upload_strategy",
    "is_data_plane_strategy",
    "VALID_STRATEGIES",
    "STRATEGY_EXECUTOR",
    "DEFAULT_LOCAL_TARGET_ROOT",
    "DEFAULT_RELAY_ROOT",
    "INGRESS_DIR",
    "STAGING_DIR",
    "NATIVE_STAGING_DIR",
    "native_staging_locator",
    "mount_absolute_path",
    "mount_webdav_root",
    "VALID_SOURCE_FORMS",
    "CapabilityDecision",
    "PlanningError",
    "SourceResolutionError",
    "SourceAccountUnavailable",
    "DestinationAccountUnavailable",
    "DestinationRequired",
    "TargetNotFound",
    "TargetDisabled",
    "TargetNotSelectable",
    "TargetIncompatible",
    "MagnetTargetRestricted",
    "NativeTransferUnavailable",
    "SourceLandingUnavailable",
    "OpenlistUnavailable",
    "UnsupportedPlanVersion",
    "PlanValidationError",
    "normalize_path",
    "job_dir",
    "path_is_safe",
    "paths_overlap",
    "local_contained",
    "namespace_kind",
    "openlist_namespace",
    "native_namespace",
    "local_namespace",
    "split_openlist_namespace",
    "path_leaf",
    "ProviderAdapter",
    "AdapterRegistry",
    "AccountsService",
    "TargetService",
    "RulesService",
    "MountService",
    "SourceResolver",
    "DestinationResolver",
    "ImportPlanner",
    "PlanValidationDeps",
    "PlanValidator",
    "validate_plan",
    "JobPlanError",
    "job_plan_reason",
    "plan_path",
    "plan_strategy",
    "validated_v1_execution_plan_from_job",
    "v1_execution_plan_from_job",
]
