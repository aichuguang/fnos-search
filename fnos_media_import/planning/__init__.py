"""V1.0 多链路 Import Planner v4 计划包。

系统只创建、校验并执行 v4 计划（``EXECUTION_PLAN_VERSION == 4``）。生成器与
校验器必须执行相同的安全约束；任何字段缺失、修订被非法删除或身份不一致都
失败关闭，绝不根据当前后台配置“补全”计划，也绝不把旧 v2/v3 计划当作可执行计划。

模块布局：

- ``source_resolver``    从服务端可信资源记录解析来源（不信任客户端 provider 字段）。
- ``destination_resolver`` 按管理员规则解析来源的目标与账号。
- ``planner``            冻结唯一执行策略与任务级路径，返回不可变 v4 计划。
- ``plan_validator``     按 §12.4 校验冻结计划与任务身份一致，失败关闭。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.models import (
    EXECUTION_PLAN_VERSION,  # noqa: F401  # re-export：本包统一使用 v4
    AccountSelection,
    OpenListMount,
    ProviderAccount,
    ProviderCapabilities,
    StorageTarget,
)

# ---- 传输策略常量（§3.3 / §12.3） ------------------------------------------

STRATEGY_NATIVE_SAVE = "native_save"
STRATEGY_NATIVE_OFFLINE = "native_offline"
STRATEGY_RCLONE_COPY = "rclone_copy"
STRATEGY_RELAY_UPLOAD_MOBILE = "relay_upload_mobile"

VALID_STRATEGIES = frozenset(
    {
        STRATEGY_NATIVE_SAVE,
        STRATEGY_NATIVE_OFFLINE,
        STRATEGY_RCLONE_COPY,
        STRATEGY_RELAY_UPLOAD_MOBILE,
    }
)

# 策略 -> 执行器（§12.3 transfer.executor）。
STRATEGY_EXECUTOR = {
    STRATEGY_NATIVE_SAVE: "native",
    STRATEGY_NATIVE_OFFLINE: "native_offline",
    STRATEGY_RCLONE_COPY: "rclone",
    STRATEGY_RELAY_UPLOAD_MOBILE: "mobile_relay",
}

# 部署级本地根（可被 config 的 FNOS_LOCAL_TARGET_ROOT / FNOS_RELAY_ROOT 覆盖）。
DEFAULT_LOCAL_TARGET_ROOT = "/storage-targets"
DEFAULT_RELAY_ROOT = "/data/relay"

# 任务级落地区 / 暂存区目录名（§8.5、§12.3）。
INGRESS_DIR = "_fnos_ingress"
STAGING_DIR = "_fnos_staging"

# 允许的来源形态（§3.3 SourceDescriptor.source_form）。
VALID_SOURCE_FORMS = frozenset({"share", "magnet", "local", "http"})

# 已知的 v4 Provider 种类。未知 Provider 一律拒绝，不允许回落到通用 rclone。
KNOWN_PROVIDER_KINDS = frozenset({"mobile", "quark", "tianyi", "sixpan", "local", "generic"})


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


# ---- 服务接口（Protocol） ---------------------------------------------------
#
# 本包只依赖这些最小接口。测试以 fakes 注入；真实实现（账号/目标/规则/OpenList
# 服务）在后续阶段按同一接口接入，无需修改本包。


class ProviderAdapter(Protocol):
    kind: str

    def capabilities(self) -> ProviderCapabilities: ...

    def can_native_transfer(
        self,
        source: Any,
        destination: Any,
        account: Any,
    ) -> CapabilityDecision: ...

    def relay_uploader(self) -> Any: ...


class AdapterRegistry(Protocol):
    def get(self, provider_kind: str) -> ProviderAdapter: ...


class AccountsService(Protocol):
    def get_selection(self, provider_kind: str, scope: str = "default") -> AccountSelection | None: ...

    def get_account(self, account_id: str) -> ProviderAccount | None: ...

    def has_revision(self, account_id: str, revision: int) -> bool: ...


class TargetService(Protocol):
    def get(self, destination_id: str) -> StorageTarget | None: ...

    def get_revision(self, destination_id: str, revision: int) -> StorageTarget | None: ...


class RulesService(Protocol):
    """来源 -> 目标规则。规则是目标选择的唯一真源。"""

    def resolve_default(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        audience: str,
    ) -> str | None: ...

    def is_allowed(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        destination_id: str,
        audience: str,
    ) -> bool: ...


class MountService(Protocol):
    def get(self, mount_id: str) -> OpenListMount | None: ...

    def find_mount_for_account(
        self,
        provider_kind: str,
        account_id: str,
        *,
        readable: bool = True,
    ) -> OpenListMount | None: ...


# 底部再导入子模块，避免循环导入：异常/常量先定义，子模块用 ``from . import``
# 引用它们时 __init__ 已部分初始化。
from .source_resolver import SourceResolver  # noqa: E402
from .destination_resolver import DestinationResolver  # noqa: E402
from .planner import ImportPlannerV4  # noqa: E402
from .plan_validator import PlanValidationDeps, PlanValidator, validate_v4_plan  # noqa: E402

__all__ = [
    "EXECUTION_PLAN_VERSION",
    "STRATEGY_NATIVE_SAVE",
    "STRATEGY_NATIVE_OFFLINE",
    "STRATEGY_RCLONE_COPY",
    "STRATEGY_RELAY_UPLOAD_MOBILE",
    "VALID_STRATEGIES",
    "STRATEGY_EXECUTOR",
    "DEFAULT_LOCAL_TARGET_ROOT",
    "DEFAULT_RELAY_ROOT",
    "INGRESS_DIR",
    "STAGING_DIR",
    "VALID_SOURCE_FORMS",
    "KNOWN_PROVIDER_KINDS",
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
    "ImportPlannerV4",
    "PlanValidationDeps",
    "PlanValidator",
    "validate_v4_plan",
]


class DefaultAdapterRegistry:
    """最小可用注册表：按 provider_kind 返回 Adapter，缺失即失败关闭。"""

    def __init__(self, adapters: dict[str, ProviderAdapter] | None = None) -> None:
        self._adapters = dict(adapters or {})

    def register(self, adapter: ProviderAdapter) -> None:
        self._adapters[adapter.kind] = adapter

    def get(self, provider_kind: str) -> ProviderAdapter:
        try:
            return self._adapters[provider_kind]
        except KeyError as exc:
            raise PlanningError(
                f"未注册 Provider Adapter：{provider_kind}", code="PROVIDER_UNAVAILABLE"
            ) from exc
