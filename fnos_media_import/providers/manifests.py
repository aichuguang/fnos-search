from __future__ import annotations

from ..domain.models import ProviderCapabilities
from .base import manifest


MOBILE_MANIFEST = manifest(
    "mobile",
    "移动云盘",
    [
        {
            "name": "phone",
            "label": "手机号",
            "type": "text",
            "required": True,
            "autocomplete": "username",
        },
        {
            "name": "host",
            "label": "服务地址",
            "type": "url",
            "required": False,
            "placeholder": "留空使用内置地址",
        },
        {
            "name": "authorization",
            "label": "Authorization",
            "type": "password",
            "secret": True,
            "required": True,
            "managed": True,
            "write_only": True,
        },
    ],
    components={"search", "inspect", "native_save", "materialize", "relay"},
    capabilities=ProviderCapabilities(
        supports_search=True,
        supports_share_inspection=True,
        supports_native_save=True,
        supports_native_offline=False,
        supports_operation_polling=True,
        supports_native_verify=True,
        supports_source_materialization=True,
        supports_relay_upload=True,
        supports_rclone_dest=False,
        supports_direct_final=True,
        supports_quota=False,
    ),
    locator_fields=[{"name": "provider_path", "label": "移动云保存目录", "required": True}],
    auth_methods=[
        {
            "kind": "managed_account",
            "label": "导入移动云授权",
            "description": "录入移动云 Web 端 Authorization: Basic 凭证；系统验证后加密保存，不配置第三方 API 服务。",
            "interactive": True,
            "input_schema": {
                "fields": [
                    {
                        "name": "phone",
                        "label": "手机号",
                        "type": "text",
                        "required": True,
                        "autocomplete": "username",
                    },
                    {
                        "name": "authorization",
                        "label": "浏览器授权信息",
                        "type": "password",
                        "secret": True,
                        "required": True,
                        "autocomplete": "off",
                        "placeholder": "Authorization: Basic ...",
                    },
                    {
                        "name": "host",
                        "label": "服务地址",
                        "type": "url",
                        "required": False,
                        "placeholder": "留空使用内置地址",
                    },
                ]
            },
        }
    ],
    search_sources=[
        {
            "search_source_id": "mobile",
            "label": "移动云盘",
            "source_form": "share",
            "source_types": ["mobile", "cloud139"],
        }
    ],
    icon_text="移",
)


QUARK_MANIFEST = manifest(
    "quark",
    "夸克网盘",
    [
        {
            "name": "cookie",
            "label": "夸克 Cookie",
            "type": "textarea",
            "secret": True,
            "required": True,
            "placeholder": "粘贴 pan.quark.cn 登录 Cookie",
        },
        {
            "name": "base_url",
            "label": "API 地址",
            "type": "url",
            "required": False,
            "default": "https://drive-pc.quark.cn",
        },
    ],
    components={"search", "inspect", "native_save", "materialize"},
    capabilities=ProviderCapabilities(
        supports_search=True,
        supports_share_inspection=True,
        supports_native_save=True,
        supports_native_offline=False,
        supports_operation_polling=True,
        supports_native_verify=True,
        supports_source_materialization=True,
        supports_relay_upload=False,
        supports_rclone_dest=True,
        supports_direct_final=True,
        supports_quota=False,
    ),
    locator_fields=[{"name": "path", "label": "夸克保存目录", "required": True}],
    auth_methods=[
        {
            "kind": "qrcode",
            "label": "\u626b\u7801\u767b\u5f55",
            "description": "\u4f7f\u7528\u5938\u514b APP \u626b\u7801\uff0c\u7cfb\u7edf\u81ea\u52a8\u83b7\u53d6\u5e76\u9a8c\u8bc1 Cookie",
            "interactive": True,
        },
    ],
    search_sources=[
        {
            "search_source_id": "quark",
            "label": "夸克网盘",
            "source_form": "share",
            "source_types": ["quark"],
        }
    ],
    icon_text="夸",
)


TIANYI_MANIFEST = manifest(
    "tianyi",
    "天翼云盘",
    [
        {
            "name": "username",
            "label": "天翼云账号",
            "type": "text",
            "required": False,
        },
        {
            "name": "password",
            "label": "天翼云密码",
            "type": "password",
            "secret": True,
            "required": False,
        },
        {
            "name": "cookie",
            "label": "SSON Cookie（备用）",
            "type": "textarea",
            "secret": True,
            "required": False,
        },
        {
            "name": "identity",
            "label": "账号标识",
            "type": "text",
            "required": False,
        },
        {
            "name": "base_url",
            "label": "API 地址",
            "type": "url",
            "required": False,
            "default": "https://cloud.189.cn",
        },
    ],
    components={"search", "inspect", "native_save", "materialize"},
    capabilities=ProviderCapabilities(
        supports_search=True,
        supports_share_inspection=True,
        supports_native_save=True,
        supports_native_offline=False,
        supports_operation_polling=True,
        supports_native_verify=True,
        supports_source_materialization=True,
        supports_relay_upload=False,
        supports_rclone_dest=True,
        supports_direct_final=True,
        supports_quota=False,
    ),
    locator_fields=[{"name": "folder_id", "label": "天翼保存目录 ID", "required": True}],
    auth_methods=[
        {
            "kind": "password",
            "label": "账号密码登录",
            "description": "使用天翼云账号密码登录；触发风控时会显示图片验证码。",
            "interactive": True,
            "input_schema": {
                "fields": [
                    {
                        "name": "username",
                        "label": "天翼云账号",
                        "type": "text",
                        "required": True,
                        "autocomplete": "username",
                        "placeholder": "手机号、邮箱或天翼账号",
                    },
                    {
                        "name": "password",
                        "label": "天翼云密码",
                        "type": "password",
                        "secret": True,
                        "required": True,
                        "autocomplete": "current-password",
                    },
                ]
            },
        }
    ],
    managed_secret_fields={
        "session_key",
        "session_secret",
        "access_token",
        "refresh_token",
        "sson",
    },
    search_sources=[
        {
            "search_source_id": "tianyi",
            "label": "天翼云盘",
            "source_form": "share",
            "source_types": ["tianyi", "cloud189"],
        }
    ],
    icon_text="翼",
)


SIXPAN_MANIFEST = manifest(
    "sixpan",
    "六盘",
    [
        {"name": "host", "label": "六盘 OpenAPI 地址", "type": "url", "required": False, "default": "openapi.2dland.cn"},
        {"name": "client_id", "label": "Client ID", "type": "text", "required": True},
        {"name": "client_secret", "label": "Client Secret", "type": "text", "secret": True, "required": True},
        {"name": "oauth_client_id", "label": "OAuth Client ID", "type": "text", "required": False},
    ],
    components={"native_offline"},
    capabilities=ProviderCapabilities(
        supports_search=False,
        supports_share_inspection=False,
        supports_native_save=False,
        supports_native_offline=True,
        supports_operation_polling=True,
        supports_native_verify=True,
        supports_source_materialization=False,
        supports_relay_upload=False,
        supports_rclone_dest=True,
        supports_direct_final=True,
        supports_quota=False,
    ),
    locator_fields=[{"name": "path", "label": "六盘保存目录", "required": True}],
    auth_methods=[
        {
            "kind": "device_code",
            "label": "设备授权",
            "description": "填写六盘应用凭据后完成设备授权；Access Token 与 Refresh Token 由系统自动获取和刷新。",
            "interactive": True,
            "input_schema": {
                "fields": [
                    {
                        "name": "host",
                        "label": "OpenAPI 地址",
                        "type": "text",
                        "required": False,
                        "default": "openapi.2dland.cn",
                    },
                    {
                        "name": "client_id",
                        "label": "Client ID",
                        "type": "text",
                        "required": True,
                    },
                    {
                        "name": "client_secret",
                        "label": "Client Secret",
                        "type": "password",
                        "secret": True,
                        "required": True,
                        "autocomplete": "off",
                    },
                    {
                        "name": "oauth_client_id",
                        "label": "OAuth Client ID",
                        "type": "text",
                        "required": False,
                        "placeholder": "留空则使用 Client ID",
                    },
                ]
            },
        }
    ],
    managed_secret_fields={"access_token", "refresh_token"},
    search_sources=[
        {
            "search_source_id": "sixpan_magnet",
            "label": "六盘磁链",
            "source_form": "magnet",
            "source_types": ["magnet", "torrent"],
        }
    ],
    icon_text="六",
)


BUILTIN_MANIFESTS = {
    item.provider_kind: item
    for item in (MOBILE_MANIFEST, QUARK_MANIFEST, TIANYI_MANIFEST, SIXPAN_MANIFEST)
}


__all__ = [
    "BUILTIN_MANIFESTS",
    "MOBILE_MANIFEST",
    "QUARK_MANIFEST",
    "SIXPAN_MANIFEST",
    "TIANYI_MANIFEST",
]
