"""OpenList 连接与挂载（connection/mount）模块。

V1.0 数据面：OpenList WebDAV 是唯一数据面。本模块维护：

- ``OpenListConnectionService``：WebDAV 服务连接配置与凭证 AES-GCM 信封加密、
  认证探测（§7.2 openlist_connections）。
- ``OpenListMountService``：账号到 WebDAV 根的 mount 映射、完整读写检测
  （§7.3）与目录选择器。
- ``RcloneLocatorResolver``：把 ``(connection_id, mount_id, relative_path)``
  解析为受控 ``remote:path``（§7.4），拒绝任意 remote 名称与配置路径。
- ``create_openlist_blueprint``：§16.2 管理 API 路由，永不返回密码。

本包只新增文件，不修改既有模块。
"""

from __future__ import annotations

__version__ = "1.0"


class OpenListError(Exception):
    """OpenList 模块基类错误。"""


class OpenListNotFoundError(OpenListError):
    """对象不存在。"""


class OpenListValidationError(OpenListError):
    """输入或配置校验失败。"""


class OpenListMasterKeyError(OpenListError):
    """缺少或无法解析 FNOS_ROOT_KEY。"""


class OpenListCredentialError(OpenListError):
    """凭证无法解密。"""


class OpenListWebDAVError(OpenListError):
    """WebDAV 请求失败。"""


__all__ = [
    "OpenListError",
    "OpenListNotFoundError",
    "OpenListValidationError",
    "OpenListMasterKeyError",
    "OpenListCredentialError",
    "OpenListWebDAVError",
]
