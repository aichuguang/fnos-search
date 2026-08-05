from __future__ import annotations

from typing import Any


def get_openapi_spec() -> dict[str, Any]:
    """返回 Swagger / OpenAPI 文档。

    项目暂不引入额外 Swagger 依赖，避免 Docker 构建再次被 pip 下载拖慢。
    这里维护一份显式 OpenAPI 3.0 描述，供 `/openapi.json` 和 `/swagger` 使用。
    """

    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "飞牛影视统一入库维护系统 API",
            "version": "0.2.0",
            "description": "PanSou 搜索、资源识别、入库任务、rclone 搬运和飞牛媒体库刷新接口。",
        },
        "servers": [{"url": "/", "description": "当前服务"}],
        "tags": [
            {"name": "Health", "description": "健康检查"},
            {"name": "Config", "description": "前端公开配置"},
            {"name": "Public", "description": "访客公开接口，只返回裁剪字段"},
            {"name": "AdminAuth", "description": "管理员登录与会话"},
            {"name": "Admin", "description": "管理员接口，需要登录会话"},
            {"name": "Search", "description": "资源搜索与链接识别"},
            {"name": "Import", "description": "入库任务"},
            {"name": "Jobs", "description": "任务中心"},
            {"name": "Rclone", "description": "rclone 搬运编排"},
            {"name": "Organizer", "description": "OpenList 标准化整理与 STRM 融合"},
            {"name": "Quark", "description": "Quark 分享检测"},
            {"name": "Cloud139", "description": "139 移动云官方直转与分享检测"},
            {"name": "Media", "description": "飞牛媒体库刷新"},
            {"name": "Callback", "description": "外部/脚本回调"},
        ],
        "paths": {
            "/health": {
                "get": {
                    "tags": ["Health"],
                    "summary": "健康检查",
                    "responses": {"200": {"description": "服务正常", "content": _json_ref("HealthResponse")}},
                }
            },
            "/livez": {
                "get": {
                    "tags": ["Health"],
                    "summary": "存活检查",
                    "responses": {"200": {"description": "进程存活", "content": _json_schema({"type": "object"})}},
                }
            },
            "/readyz": {
                "get": {
                    "tags": ["Health"],
                    "summary": "就绪检查",
                    "responses": {
                        "200": {"description": "服务已就绪", "content": _json_schema({"type": "object"})},
                        "503": {"description": "数据库或依赖未就绪", "content": _json_schema({"type": "object"})},
                    },
                }
            },
            "/dependencies": {
                "get": {
                    "tags": ["Admin"],
                    "summary": "查询依赖服务状态",
                    "security": [{"cookieAuth": []}],
                    "responses": {
                        "200": {"description": "依赖状态", "content": _json_schema({"type": "object"})},
                        "401": {"description": "管理员未登录", "content": _json_ref("ErrorResponse")},
                    },
                }
            },
            "/api/config/public": {
                "get": {
                    "tags": ["Config"],
                    "summary": "获取前端公开配置",
                    "responses": {"200": {"description": "公开配置", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/config": {
                "get": {
                    "tags": ["Public"],
                    "summary": "获取访客端公开配置",
                    "responses": {"200": {"description": "访客端公开配置", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/trending": {
                "get": {
                    "tags": ["Public"],
                    "summary": "Get the public daily hot-content ranking",
                    "responses": {"200": {"description": "Public ranked hot-content groups", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/search": {
                "post": {
                    "tags": ["Public"],
                    "summary": "访客搜索影视资源，只返回裁剪字段",
                    "requestBody": _body_ref("SearchRequest"),
                    "responses": {"200": {"description": "公开搜索结果", "content": _json_ref("PublicSearchResponse")}},
                }
            },
            "/api/public/detect": {
                "post": {
                    "tags": ["Public"],
                    "summary": "访客链接识别，只返回是否可提交",
                    "requestBody": _body_ref("DetectRequest"),
                    "responses": {"200": {"description": "公开识别结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/resources/{public_id}/detail": {
                "get": {
                    "tags": ["Public"],
                    "summary": "访客查看搜索资源详情，Quark / 139 来源会调用详情检测接口",
                    "parameters": [_path("public_id", "string", "公开搜索返回的资源 ID")],
                    "responses": {"200": {"description": "公开资源详情", "content": _json_ref("PublicResourceDetailResponse")}},
                }
            },
            "/api/public/resources/{public_id}/files": {
                "get": {
                    "tags": ["Public"],
                    "summary": "访客展开 Quark / 139 目录预览",
                    "parameters": [
                        _path("public_id", "string", "公开搜索返回的资源 ID"),
                        _query("fid", "string", "目录 ID"),
                    ],
                    "responses": {"200": {"description": "目录预览", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/captcha": {
                "get": {
                    "tags": ["Public"],
                    "summary": "获取访客提交验证码挑战",
                    "responses": {"200": {"description": "验证码配置或挑战", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/submit": {
                "post": {
                    "tags": ["Public"],
                    "summary": "访客提交资源入库",
                    "requestBody": _body_ref("PublicSubmitRequest"),
                    "responses": {"200": {"description": "提交结果", "content": _json_ref("PublicSubmitResponse")}},
                }
            },
            "/api/public/request/{token}": {
                "get": {
                    "tags": ["Public"],
                    "summary": "访客查询简化提交状态",
                    "parameters": [_path("token", "string", "提交编号")],
                    "responses": {"200": {"description": "简化状态", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/public/notifications/verify/{token}": {
                "get": {
                    "tags": ["Public"],
                    "summary": "显示访客邮箱验证确认页",
                    "parameters": [_path("token", "string", "一次性验证令牌")],
                    "responses": {"200": {"description": "确认页"}},
                },
                "post": {
                    "tags": ["Public"],
                    "summary": "确认访客邮箱验证",
                    "parameters": [_path("token", "string", "一次性验证令牌")],
                    "responses": {"302": {"description": "跳转到申请状态页"}},
                },
            },
            "/api/public/notifications/unsubscribe/{token}": {
                "get": {
                    "tags": ["Public"],
                    "summary": "显示访客邮件退订确认页",
                    "parameters": [_path("token", "string", "退订令牌")],
                    "responses": {"200": {"description": "确认页"}},
                },
                "post": {
                    "tags": ["Public"],
                    "summary": "确认停止接收访客邮件通知",
                    "parameters": [_path("token", "string", "退订令牌")],
                    "responses": {"302": {"description": "跳转到申请状态页"}},
                },
            },
            "/api/admin/login": {
                "post": {
                    "tags": ["AdminAuth"],
                    "summary": "管理员登录",
                    "requestBody": _body_ref("AdminLoginRequest"),
                    "responses": {
                        "200": {"description": "登录成功", "content": _json_schema({"type": "object"})},
                        "401": {"description": "登录失败", "content": _json_ref("ErrorResponse")},
                    },
                }
            },
            "/api/admin/logout": {
                "post": {
                    "tags": ["AdminAuth"],
                    "summary": "管理员退出登录",
                    "responses": {"200": {"description": "退出成功", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/admin/session": {
                "get": {
                    "tags": ["AdminAuth"],
                    "summary": "查询管理员登录态",
                    "responses": {"200": {"description": "登录态", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/admin/dashboard": _admin_get("管理员概览"),
            "/api/admin/requests": _admin_get("管理员查询访客提交列表", parameters=[_query("limit", "integer", "返回数量"), _query("status", "string", "状态过滤")]),
            "/api/admin/requests/{request_id}": _admin_get("管理员查询访客提交详情", parameters=[_path("request_id", "integer", "访客提交 ID")]),
            "/api/admin/requests/{request_id}/approve": _admin_post("管理员批准访客提交并创建正式任务", parameters=[_path("request_id", "integer", "访客提交 ID")]),
            "/api/admin/requests/{request_id}/reject": _admin_post("管理员拒绝访客提交", parameters=[_path("request_id", "integer", "访客提交 ID")]),
            "/api/admin/requests/{request_id}/cancel": _admin_post("管理员取消访客提交并清理关联搬运文件", parameters=[_path("request_id", "integer", "访客提交 ID")], request_body="CancelRequest"),
            "/api/admin/jobs": _admin_get(
                "管理员查询任务列表",
                parameters=[
                    _query("limit", "integer", "返回数量"),
                    _query("status", "string", "状态过滤"),
                    _query("category", "string", "分类过滤"),
                    _query("source_type", "string", "来源类型过滤"),
                    _query("keyword", "string", "关键词搜索"),
                ],
            ),
            "/api/admin/jobs/{job_id}": {
                **_admin_get("管理员查询任务详情", parameters=[_path("job_id", "integer", "任务 ID")]),
                **_admin_delete("删除已完成或已取消的任务记录", parameters=[_path("job_id", "integer", "任务 ID")]),
            },
            "/api/admin/jobs/{job_id}/retry": _admin_post("管理员重试任务", parameters=[_path("job_id", "integer", "任务 ID")]),
            "/api/admin/jobs/{job_id}/cancel": _admin_post("管理员取消任务并清理源端/临时缓存", parameters=[_path("job_id", "integer", "任务 ID")], request_body="CancelRequest"),
            "/api/admin/jobs/batch-retry": _admin_post("管理员批量重试任务", request_body="BatchRetryRequest"),
            "/api/admin/media/refresh": _admin_post("管理员触发媒体库刷新", request_body="MediaRefreshRequest"),
            "/api/admin/rclone/status": _admin_get("管理员查询 rclone 状态"),
            "/api/admin/rclone/check": _admin_get("管理员检查 rclone 环境"),
            "/api/admin/rclone/start": _admin_post("管理员启动 rclone 搬运", request_body="RcloneStartRequest"),
            "/api/admin/rclone/stop": _admin_post("管理员停止 rclone 搬运"),
            "/api/admin/rclone/logs": _admin_get("管理员查询 rclone 日志", parameters=[_query("limit", "integer", "日志行数")]),
            "/api/admin/rclone/runs": _admin_get("管理员查询 rclone 运行记录", parameters=[_query("limit", "integer", "返回数量")]),
            "/api/admin/rclone/events": _admin_get(
                "管理员查询 rclone 事件",
                parameters=[_query("run_id", "integer", "运行 ID"), _query("limit", "integer", "返回数量")],
            ),
            "/api/admin/rclone/files": _admin_get(
                "管理员查询 rclone 文件级搬运记录",
                parameters=[
                    _query("run_id", "integer", "运行 ID"),
                    _query("job_id", "integer", "任务 ID"),
                    _query("status", "string", "搬运状态"),
                    _query("category", "string", "分类过滤"),
                    _query("limit", "integer", "返回数量"),
                ],
            ),
            "/api/admin/rclone/files/{event_id}/retry": _admin_post(
                "管理员单独重试失败文件搬运",
                parameters=[_path("event_id", "integer", "rclone 文件事件 ID")],
                request_body="RcloneFileRetryRequest",
            ),
            "/api/admin/organizer/tasks": _admin_get(
                "管理员查询 OpenList 标准化任务",
                parameters=[_query("limit", "integer", "返回数量"), _query("status", "string", "状态过滤")],
                tag="Organizer",
            ),
            "/api/admin/organizer/tasks/scan": _admin_post("管理员手动创建 OpenList 扫描任务", request_body="OrganizerScanRequest", tag="Organizer"),
            "/api/admin/organizer/tasks/{task_id}": {
                **_admin_get("管理员查看 OpenList 标准化任务详情", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
                **_admin_delete("删除已完成或已取消的标准化记录", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            },
            "/api/admin/organizer/tasks/{task_id}/rebuild": _admin_post("管理员重建标准化计划", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            "/api/admin/organizer/tasks/{task_id}/mappings/{mapping_id}": _admin_patch(
                "管理员编辑标准化映射",
                parameters=[_path("task_id", "integer", "标准化任务 ID"), _path("mapping_id", "integer", "映射 ID")],
                request_body="OrganizerMappingUpdateRequest",
                tag="Organizer",
            ),
            "/api/admin/organizer/tasks/{task_id}/mappings/batch": _admin_post(
                "管理员原子批量编辑标准化映射",
                parameters=[_path("task_id", "integer", "标准化任务 ID")],
                request_body="OrganizerMappingsBatchUpdateRequest",
                tag="Organizer",
            ),
            "/api/admin/organizer/tasks/{task_id}/approve": _admin_post("管理员确认放行标准化任务", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            "/api/admin/organizer/tasks/{task_id}/apply": _admin_post("管理员执行标准化任务", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            "/api/admin/organizer/tasks/{task_id}/skip": _admin_post("管理员跳过命名整理并将暂存内容原名移入正式分类目录", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            "/api/admin/organizer/tasks/{task_id}/retry": _admin_post("管理员重试标准化任务", parameters=[_path("task_id", "integer", "标准化任务 ID")], tag="Organizer"),
            "/api/admin/organizer/runs": _admin_get("管理员查询标准化执行记录", parameters=[_query("limit", "integer", "返回数量")], tag="Organizer"),
            "/api/admin/organizer/runs/{run_id}/rollback": _admin_post("管理员回滚标准化执行", parameters=[_path("run_id", "integer", "执行记录 ID")], tag="Organizer"),
            "/api/admin/btbtla/proxy-test": _admin_post("管理员检测 BTBTLA 代理链路", request_body="BtbtlaProxyTestRequest"),
            "/api/admin/openlist/test": _admin_post("管理员测试 OpenList API", tag="Organizer"),
            "/api/admin/tmdb/test": _admin_post("管理员测试 TMDB API", tag="Organizer"),
            "/api/admin/ai/test": _admin_post("管理员测试 AI 校准 API", tag="Organizer"),
            "/api/admin/config": _admin_get("管理员查询脱敏配置"),
            "/api/admin/security/status": _admin_get("管理员查询安全状态与风险提示"),
            "/api/admin/advanced-config": {
                **_admin_get("管理员查询数据库持久化高级配置"),
                **_admin_post("管理员保存数据库持久化高级配置", request_body="AdvancedConfigRequest"),
            },
            "/api/admin/advanced-config/export": _admin_post(
                "管理员导出含敏感密钥的版本化高级配置备份",
                request_body="AdvancedConfigExportRequest",
            ),
            "/api/admin/settings": {
                **_admin_get("管理员查询系统设置"),
                **_admin_post("管理员保存系统设置", request_body="AdminSettingsRequest"),
            },
            "/api/admin/notifications": {
                **_admin_get("管理员查询脱敏通知配置与投递摘要"),
                **_admin_post("管理员保存通知配置"),
            },
            "/api/admin/notifications/test": _admin_post("管理员测试通知渠道"),
            "/api/admin/notifications/deliveries": _admin_get(
                "管理员查询通知投递审计",
                parameters=[
                    _query("event_type", "string", "事件类型"),
                    _query("channel", "string", "渠道"),
                    _query("limit", "integer", "返回数量"),
                    _query("offset", "integer", "偏移量"),
                ],
            ),
            "/api/admin/notifications/tasks/{task_id}/retry": _admin_post(
                "管理员重新入队失败的通知任务",
                parameters=[_path("task_id", "integer", "Worker 任务 ID")],
            ),
            "/api/admin/rclone/webdav-config": {
                **_admin_get(
                    "管理员查询 rclone WebDAV 配置状态",
                    parameters=[_query("remote_name", "string", "rclone Remote 名称")],
                    tag="Rclone",
                ),
                **_admin_post(
                    "管理员保存并检测 rclone WebDAV 配置",
                    request_body="RcloneWebdavConfigRequest",
                    tag="Rclone",
                ),
            },
            "/api/admin/rclone/webdav-config/test": _admin_post(
                "管理员检测已保存的 rclone WebDAV 连接",
                request_body="RcloneWebdavTestRequest",
                tag="Rclone",
            ),
            "/api/admin/search/providers": {
                **_admin_get("管理员查询搜索源聚合配置"),
                **_admin_post("管理员保存搜索源启用和优先级", request_body="SearchProvidersRequest"),
            },
            "/api/admin/search/aliases": {
                **_admin_get("管理员查询搜索别名词库"),
                **_admin_post("管理员保存搜索别名词库", request_body="SearchAliasesRequest"),
            },
            "/api/admin/adapters": _admin_get("管理员查询多线路适配器占位状态"),
            "/api/admin/adapters/{adapter_key}/probe": _admin_post(
                "管理员触发线路适配器占位检查",
                parameters=[_path("adapter_key", "string", "适配器 key，例如 cloud139/cloud189/sixpan")],
            ),
            "/api/admin/sixpan/tasks": _admin_get(
                "管理员查询六盘离线任务列表",
                parameters=[_query("limit", "integer", "返回数量")],
            ),
            "/api/admin/sixpan/probe": _admin_post("管理员检测六盘账号授权状态"),
            "/api/admin/sixpan/sync": _admin_post("管理员手动同步六盘离线任务状态"),
            "/api/admin/sixpan/jobs/{job_id}/retry-media-refresh": _admin_post(
                "管理员仅重试已完成六盘任务的飞牛媒体库刷新",
                parameters=[_path("job_id", "integer", "入库任务 ID")],
            ),
            "/api/admin/sixpan/oauth/device-code": _admin_post("管理员创建六盘 device_code 授权入口"),
            "/api/admin/sixpan/oauth/device-code/check": _admin_post("管理员检查六盘 device_code 授权状态并保存 token"),
            "/api/search": {
                "post": {
                    "tags": ["Search"],
                    "summary": "搜索影视资源",
                    "requestBody": _body_ref("SearchRequest"),
                    "responses": {"200": {"description": "搜索结果", "content": _json_ref("SearchResponse")}},
                }
            },
            "/api/detect": {
                "post": {
                    "tags": ["Search"],
                    "summary": "识别资源链接类型和入库线路",
                    "requestBody": _body_ref("DetectRequest"),
                    "responses": {"200": {"description": "识别结果", "content": _json_ref("DetectResponse")}},
                }
            },
            "/api/import": {
                "post": {
                    "tags": ["Import"],
                    "summary": "创建入库任务",
                    "requestBody": _body_ref("ImportRequest"),
                    "responses": {"200": {"description": "入库结果", "content": _json_ref("JobMutationResponse")}},
                }
            },
            "/api/jobs": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "查询任务列表",
                    "parameters": [
                        _query("limit", "integer", "返回数量，默认 100，范围 1-500"),
                        _query("status", "string", "按任务状态过滤"),
                        _query("category", "string", "按分类过滤"),
                        _query("source_type", "string", "按来源类型过滤"),
                        _query("keyword", "string", "关键词搜索"),
                    ],
                    "responses": {"200": {"description": "任务列表", "content": _json_ref("JobListResponse")}},
                }
            },
            "/api/jobs/{job_id}": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "查询任务详情和事件",
                    "parameters": [_path("job_id", "integer", "任务 ID")],
                    "responses": {
                        "200": {"description": "任务详情", "content": _json_schema({"type": "object"})},
                        "404": {"description": "任务不存在", "content": _json_ref("ErrorResponse")},
                    },
                }
            },
            "/api/jobs/{job_id}/retry": {
                "post": {
                    "tags": ["Jobs"],
                    "summary": "重试任务",
                    "parameters": [_path("job_id", "integer", "任务 ID")],
                    "responses": {"200": {"description": "重试结果", "content": _json_ref("JobMutationResponse")}},
                }
            },
            "/api/media/refresh": {
                "post": {
                    "tags": ["Media"],
                    "summary": "触发飞牛媒体库刷新",
                    "requestBody": _body_ref("MediaRefreshRequest"),
                    "responses": {"200": {"description": "刷新结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/status": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "查询 rclone 搬运状态",
                    "responses": {"200": {"description": "rclone 状态", "content": _json_ref("RcloneStatusResponse")}},
                }
            },
            "/api/rclone/start": {
                "post": {
                    "tags": ["Rclone"],
                    "summary": "启动 rclone 搬运",
                    "requestBody": _body_ref("RcloneStartRequest"),
                    "responses": {"200": {"description": "启动结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/stop": {
                "post": {
                    "tags": ["Rclone"],
                    "summary": "停止 rclone 搬运",
                    "responses": {"200": {"description": "停止结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/logs": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "查询 rclone 搬运日志",
                    "parameters": [_query("limit", "integer", "日志行数，默认 200，范围 1-1000")],
                    "responses": {"200": {"description": "日志列表", "content": _json_ref("StringListResponse")}},
                }
            },
            "/api/rclone/runs": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "查询 rclone 运行记录",
                    "parameters": [_query("limit", "integer", "返回数量，默认 50，范围 1-200")],
                    "responses": {"200": {"description": "运行记录", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/events": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "查询 rclone 事件",
                    "parameters": [
                        _query("run_id", "integer", "运行 ID，可选"),
                        _query("limit", "integer", "返回数量，默认 200，范围 1-1000"),
                    ],
                    "responses": {"200": {"description": "事件列表", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/files": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "查询 rclone 文件级搬运记录",
                    "parameters": [
                        _query("run_id", "integer", "运行 ID，可选"),
                        _query("job_id", "integer", "任务 ID，可选"),
                        _query("status", "string", "搬运状态，可选"),
                        _query("category", "string", "分类过滤，可选"),
                        _query("limit", "integer", "返回数量，默认 200，范围 1-1000"),
                    ],
                    "responses": {"200": {"description": "文件级搬运记录", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/rclone/check": {
                "get": {
                    "tags": ["Rclone"],
                    "summary": "检查 rclone 编排环境",
                    "responses": {"200": {"description": "环境检查结果", "content": _json_ref("RcloneCheckResponse")}},
                }
            },
            "/api/quark/check": {
                "post": {
                    "tags": ["Quark"],
                    "summary": "检测 Quark 分享有效性",
                    "requestBody": _body_ref("QuarkCheckRequest"),
                    "responses": {"200": {"description": "检测结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/quark/file-list": {
                "post": {
                    "tags": ["Quark"],
                    "summary": "查询 Quark 分享文件列表",
                    "requestBody": _body_ref("QuarkFileListRequest"),
                    "responses": {"200": {"description": "文件列表", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/cloud139/check": {
                "post": {
                    "tags": ["Cloud139"],
                    "summary": "检测 139 移动云分享有效性",
                    "requestBody": _body_ref("Cloud139CheckRequest"),
                    "responses": {"200": {"description": "检测结果", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/cloud139/file-list": {
                "post": {
                    "tags": ["Cloud139"],
                    "summary": "查询 139 移动云分享文件列表",
                    "requestBody": _body_ref("Cloud139FileListRequest"),
                    "responses": {"200": {"description": "文件列表", "content": _json_schema({"type": "object"})}},
                }
            },
            "/api/callback/rclone": {
                "post": {
                    "tags": ["Callback"],
                    "summary": "rclone 脚本回调任务状态",
                    "requestBody": _body_ref("RcloneCallbackRequest"),
                    "responses": {
                        "200": {"description": "回调处理结果", "content": _json_schema({"type": "object"})},
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "tags": ["Config"],
                    "summary": "获取 OpenAPI JSON",
                    "responses": {"200": {"description": "OpenAPI 3.0 文档", "content": _json_schema({"type": "object"})}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "session",
                    "description": "管理员登录后由 Flask session cookie 提供。",
                }
            },
            "schemas": {
                "HealthResponse": _object({"ok": {"type": "boolean"}, "name": {"type": "string"}}),
                "ErrorResponse": _object({"success": {"type": "boolean", "example": False}, "message": {"type": "string"}}),
                "SearchRequest": _object(
                    {
                        "keyword": {"type": "string", "description": "搜索关键词"},
                        "kw": {"type": "string", "description": "搜索关键词别名"},
                        "sources": {"type": "array", "items": {"type": "string"}, "description": "可选搜索源"},
                        "token": {"type": "string", "description": "临时 PanSou Token"},
                    },
                    required=["keyword"],
                ),
                "SearchResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "items": {"type": "array", "items": {"type": "object"}},
                        "raw": {"type": "object"},
                    }
                ),
                "PublicSearchResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "public_id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "supported": {"type": "boolean"},
                                    "reason": {"type": "string"},
                                    "datetime": {"type": "string"},
                                    "size_text": {"type": "string"},
                                    "category_suggestion": {"type": "object"},
                                },
                            },
                        },
                    }
                ),
                "PublicResourceDetailResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "detail": {
                            "type": "object",
                            "properties": {
                                "public_id": {"type": "string"},
                                "title": {"type": "string"},
                                "source_type": {"type": "string"},
                                "category_suggestion": {"type": "object"},
                                "inspection": {"type": "object", "description": "Quark / 139 检测摘要或其他来源预留接口说明"},
                            },
                        },
                    }
                ),
                "DetectRequest": _object(
                    {"url": {"type": "string"}, "password": {"type": "string"}},
                    required=["url"],
                ),
                "DetectResponse": _object({"success": {"type": "boolean"}, "link": {"type": "object"}}),
                "ImportRequest": _object(
                    {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "source_url": {"type": "string"},
                        "password": {"type": "string"},
                        "category": {"type": "string", "enum": ["movie", "tv", "anime", "variety", "other"]},
                    },
                    required=["url"],
                ),
                "PublicSubmitRequest": _object(
                    {
                        "public_id": {"type": "string", "description": "公开搜索返回的资源 ID，例如 RS-123"},
                        "resource_id": {"type": "string", "description": "public_id 别名"},
                        "url": {"type": "string", "description": "手动链接提交时使用"},
                        "password": {"type": "string"},
                        "category": {"type": "string", "enum": ["movie", "tv", "anime", "variety", "other"]},
                        "preferred_title": {"type": "string"},
                        "note": {"type": "string"},
                        "captcha_answer": {"type": "string", "description": "验证码答案"},
                        "notification_email_enabled": {"type": "boolean", "description": "是否订阅邮件进度"},
                        "notification_email": {"type": "string", "format": "email"},
                    }
                ),
                "PublicSubmitResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "message": {"type": "string"},
                        "request_token": {"type": "string"},
                        "status": {"type": "string"},
                    }
                ),
                "AdminLoginRequest": _object(
                    {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                    },
                    required=["username", "password"],
                ),
                "JobMutationResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "created": {"type": "boolean"},
                        "message": {"type": "string"},
                        "job": {"type": "object"},
                        "status_label": {"type": "string"},
                    }
                ),
                "BatchRetryRequest": _object(
                    {
                        "job_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "最多一次处理 50 个任务 ID",
                        }
                    },
                    required=["job_ids"],
                ),
                "JobListResponse": _object({"success": {"type": "boolean"}, "items": {"type": "array", "items": {"type": "object"}}}),
                "CancelRequest": _object(
                    {
                        "reason": {"type": "string", "description": "取消或拒绝原因"},
                        "cleanup": {"type": "boolean", "description": "是否执行文件清理，默认 true"},
                        "delete_source": {"type": "boolean", "description": "删除源端待搬运文件，默认 true"},
                        "delete_temp": {"type": "boolean", "description": "删除 rclone 容器本地 temp 缓存，默认 true"},
                        "delete_target_partial": {"type": "boolean", "description": "删除已有文件级记录对应的目标端残留，默认 true"},
                        "stop_running": {"type": "boolean", "description": "取消时是否停止正在运行的 rclone 搬运脚本"},
                    }
                ),
                "AdvancedConfigRequest": _object(
                    {
                        "config": {
                            "type": "object",
                            "description": "可持久化高级配置或版本化导出文档。merge 模式下敏感字段留空表示不修改；replace 模式下空值会清除旧密钥。",
                            "additionalProperties": True,
                        },
                        "mode": {"type": "string", "enum": ["merge", "replace"], "default": "merge"},
                        "source": {"type": "string", "enum": ["editor", "import"]},
                        "scope": {"type": "string", "enum": ["stored", "effective"], "default": "stored"},
                    }
                ),
                "AdvancedConfigExportRequest": _object(
                    {
                        "confirm": {
                            "type": "boolean",
                            "description": "确认导出内容包含 Token、密码等敏感字段。",
                        }
                    },
                    required=["confirm"],
                ),
                "BtbtlaProxyTestRequest": _object(
                    {
                        "btbtla": {
                            "type": "object",
                            "properties": {
                                "base_url": {"type": "string"},
                                "timeout": {"type": "integer"},
                                "request_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                                "retry_delay_seconds": {"type": "number", "minimum": 0, "maximum": 5},
                                "verify_tls": {"type": "boolean"},
                                "use_env_proxy": {"type": "boolean"},
                                "proxy_enabled": {"type": "boolean"},
                                "proxy_url": {
                                    "type": "string",
                                    "description": "支持 http、https、socks5、socks5h；留空时复用已保存的脱敏配置。",
                                },
                            },
                        }
                    }
                ),
                "AdminSettingsRequest": _object(
                    {
                        "public": {
                            "type": "object",
                            "properties": {
                                "allow_anonymous_search": {"type": "boolean"},
                                "request_query_enabled": {"type": "boolean"},
                                "hide_full_links": {"type": "boolean"},
                            },
                        },
                        "submission": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "enum": ["auto", "review", "mixed"]},
                            },
                        },
                    }
                ),
                "SearchProvidersRequest": _object(
                    {
                        "providers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string", "example": "pansou"},
                                    "enabled": {"type": "boolean"},
                                    "priority": {"type": "integer", "description": "数字越小优先级越高"},
                                },
                            },
                        }
                    }
                ),
                "SearchAliasesRequest": _object(
                    {
                        "aliases": {
                            "type": "object",
                            "description": "标准片名到别名数组的映射，例如 {'鬼子来了': ['鬼子laile','GZLL']}",
                            "additionalProperties": {"type": "array", "items": {"type": "string"}},
                        }
                    }
                ),
                "MediaRefreshRequest": _object({"category": {"type": "string", "enum": ["movie", "tv", "anime", "variety", "other"]}}),
                "RcloneStartRequest": _object({"reason": {"type": "string", "example": "web_manual"}}),
                "RcloneFileRetryRequest": _object({"force": {"type": "boolean", "description": "非失败状态也强制重试"}}),
                "OrganizerScanRequest": _object(
                    {
                        "category": {"type": "string", "enum": ["movie", "tv", "anime", "variety", "other"]},
                        "path": {"type": "string", "description": "OpenList 虚拟目录路径"},
                        "title": {"type": "string", "description": "可选搜索名/标题"},
                        "auto_apply": {"type": "boolean", "description": "高置信时是否自动执行"},
                    },
                    required=["path"],
                ),
                "OrganizerMappingUpdateRequest": _object(
                    {
                        "target_path": {"type": "string"},
                        "media_type": {"type": "string", "enum": ["movie", "tv"]},
                        "title": {"type": "string"},
                        "year": {"type": "string"},
                        "season": {"type": "integer", "nullable": True},
                        "episode": {"type": "integer", "nullable": True},
                        "tmdb_id": {"type": "integer", "nullable": True},
                        "status": {"type": "string", "enum": ["ready", "need_edit", "conflict", "skipped"]},
                    }
                ),
                "OrganizerMappingsBatchUpdateRequest": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "统一修改后的片名"},
                        "season": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 99,
                            "description": "统一修改后的季号",
                        },
                    },
                    "anyOf": [{"required": ["title"]}, {"required": ["season"]}],
                },
                "RcloneStatusResponse": _object({"success": {"type": "boolean"}, "status": {"type": "object"}}),
                "StringListResponse": _object({"success": {"type": "boolean"}, "items": {"type": "array", "items": {"type": "string"}}}),
                "RcloneCheckResponse": _object(
                    {
                        "success": {"type": "boolean"},
                        "message": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "object"}},
                    }
                ),
                "QuarkCheckRequest": _object(
                    {"url": {"type": "string"}, "shareurl": {"type": "string"}, "title": {"type": "string"}, "taskname": {"type": "string"}},
                    required=["url"],
                ),
                "QuarkFileListRequest": _object(
                    {"pwd_id": {"type": "string"}, "fid": {"type": "string"}, "stoken": {"type": "string"}},
                    required=["pwd_id"],
                ),
                "Cloud139CheckRequest": _object(
                    {"url": {"type": "string"}, "shareurl": {"type": "string"}, "title": {"type": "string"}, "taskname": {"type": "string"}, "password": {"type": "string"}, "pwd": {"type": "string"}},
                    required=["url"],
                ),
                "Cloud139FileListRequest": _object(
                    {"url": {"type": "string"}, "shareurl": {"type": "string"}, "fid": {"type": "string"}, "folder_id": {"type": "string"}, "catalog_id": {"type": "string"}, "password": {"type": "string"}, "pwd": {"type": "string"}},
                    required=["url", "fid"],
                ),
                "RcloneCallbackRequest": _object(
                    {
                        "run_id": {"type": "integer"},
                        "job_id": {"type": "integer"},
                        "status": {"type": "string"},
                        "category": {"type": "string"},
                        "filename": {"type": "string"},
                        "source_path": {"type": "string"},
                        "target_path": {"type": "string"},
                        "retry_event_id": {"type": "integer"},
                        "message": {"type": "string"},
                    }
                ),
                "RcloneWebdavConfigRequest": _object(
                    {
                        "remote_name": {"type": "string", "example": "MP"},
                        "url": {"type": "string", "example": "http://host.docker.internal:5244/dav"},
                        "username": {"type": "string"},
                        "password": {"type": "string", "format": "password", "description": "留空时保留已保存密码"},
                    },
                    required=["remote_name", "url", "username"],
                ),
                "RcloneWebdavTestRequest": _object(
                    {"remote_name": {"type": "string", "example": "MP"}}
                ),
            }
        },
    }
    _complete_openapi_contract(spec)
    return spec


def _complete_openapi_contract(spec: dict[str, Any]) -> None:
    paths = spec["paths"]
    admin_operations = {
        ("/api/admin/advanced-config", "put"): "更新管理员高级配置",
        ("/api/admin/maintenance/cleanup-history", "post"): "清理历史数据",
        ("/api/admin/maintenance/history-summary", "get"): "查询历史数据统计",
        ("/api/admin/media/libraries", "get"): "查询媒体库",
        ("/api/admin/media/refresh-logs", "get"): "查询媒体库刷新日志",
        ("/api/admin/media/running", "get"): "查询运行中的媒体库刷新任务",
        ("/api/admin/openlist/dirs", "get"): "查询 OpenList 目录",
        ("/api/admin/profile", "get"): "查询管理员资料",
        ("/api/admin/profile", "post"): "更新管理员资料",
        ("/api/admin/profile", "put"): "更新管理员资料",
        ("/api/admin/profile/avatar", "post"): "上传管理员头像",
        ("/api/admin/search/aliases", "put"): "更新搜索别名",
        ("/api/admin/search/providers", "put"): "更新搜索源配置",
        ("/api/admin/settings", "put"): "更新系统设置",
        ("/api/admin/site-logo", "post"): "上传站点 Logo",
        ("/api/admin/system/events", "get"): "查询系统业务事件",
        ("/api/admin/system/task-logs", "get"): "按任务查询完整日志索引",
        ("/api/admin/system/logs", "get"): "查询系统日志",
        ("/api/admin/tmdb/search", "get"): "搜索 TMDB 媒体",
        ("/api/admin/tmdb/{media_type}/{tmdb_id}", "get"): "查询 TMDB 媒体详情",
        ("/api/admin/update-candidates", "get"): "查询追更候选",
        ("/api/admin/update-candidates/{candidate_id}/import", "post"): "导入追更候选",
        ("/api/admin/update-candidates/{candidate_id}/reject", "post"): "拒绝追更候选",
        ("/api/admin/update-runs", "get"): "查询追更运行记录",
        ("/api/admin/update-runs/{run_id}", "get"): "查询追更运行详情",
        ("/api/admin/update-scheduler/run-due", "post"): "立即执行到期追更",
        ("/api/admin/update-scheduler/status", "get"): "查询追更调度器状态",
        ("/api/admin/update-subscriptions", "get"): "查询追更订阅",
        ("/api/admin/update-subscriptions", "post"): "创建追更订阅",
        ("/api/admin/update-subscriptions/{subscription_id}", "delete"): "删除追更订阅",
        ("/api/admin/update-subscriptions/{subscription_id}", "get"): "查询追更订阅详情",
        ("/api/admin/update-subscriptions/{subscription_id}", "put"): "更新追更订阅",
        ("/api/admin/update-subscriptions/{subscription_id}/enable", "post"): "启用追更订阅",
        ("/api/admin/update-subscriptions/{subscription_id}/pause", "post"): "暂停追更订阅",
        ("/api/admin/update-subscriptions/{subscription_id}/preview", "post"): "预览追更来源",
        ("/api/admin/update-subscriptions/{subscription_id}/refresh-snapshot", "post"): "刷新追更路径快照",
        ("/api/admin/update-subscriptions/{subscription_id}/run", "post"): "立即运行追更订阅",
        ("/api/admin/trending/status", "get"): "查询每日热榜发现状态",
        ("/api/admin/trending/run", "post"): "立即执行每日热榜发现",
        ("/api/admin/trending/runs", "get"): "查询每日热榜运行记录",
        ("/api/admin/trending/candidates", "get"): "查询每日热榜候选",
        ("/api/admin/trending/candidates/{candidate_id}", "get"): "查询每日热榜候选详情",
        ("/api/admin/trending/candidates/{candidate_id}/search", "post"): "搜索热榜候选资源",
        ("/api/admin/trending/candidates/{candidate_id}/resources/{public_id}/detail", "get"): "Get hot-candidate resource detail",
        ("/api/admin/trending/candidates/{candidate_id}/resources/{public_id}/files", "get"): "List hot-candidate resource files",
        ("/api/admin/trending/candidates/{candidate_id}/import", "post"): "创建热榜候选首入库任务",
        ("/api/admin/trending/candidates/{candidate_id}/subscribe", "post"): "幂等创建或绑定热榜候选追更订阅",
        ("/api/admin/trending/candidates/{candidate_id}/ignore", "post"): "忽略每日热榜候选",
        ("/api/admin/trending/candidates/{candidate_id}/restore", "post"): "恢复每日热榜候选",
    }
    for (path, method), summary in admin_operations.items():
        path_item = paths.setdefault(path, {})
        path_item.setdefault(method, _contract_operation(summary, path, admin=True))

    public_operations = {
        ("/api/public/btbtla/resolve", "post"): "解析 BTBTLA 下载资源",
        ("/api/public/manual/preview", "post"): "预览手工提交资源",
        ("/api/public/sixpan/parse", "post"): "解析六盘离线资源",
    }
    for (path, method), summary in public_operations.items():
        paths.setdefault(path, {}).setdefault(method, _contract_operation(summary, path, admin=False))

    for path, path_item in paths.items():
        if not path.startswith("/api/admin/"):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation.setdefault("security", [{"cookieAuth": []}])
            responses = operation.setdefault("responses", {})
            responses.setdefault("200", _response("请求成功"))
            responses.setdefault("400", _response("请求参数校验失败", "ErrorResponse"))
            responses.setdefault("401", _response("管理员未登录", "ErrorResponse"))
            responses.setdefault("500", _response("服务内部错误", "ErrorResponse"))


def _contract_operation(summary: str, path: str, *, admin: bool) -> dict[str, Any]:
    parameters = []
    for name in ("subscription_id", "candidate_id", "run_id", "tmdb_id"):
        if "{" + name + "}" in path:
            parameters.append(_path(name, "integer", f"{name} 标识"))
    if "{media_type}" in path:
        parameters.append(_path("media_type", "string", "TMDB 媒体类型"))
    operation = {
        "tags": ["Admin" if admin else "Public"],
        "summary": summary,
        "parameters": parameters,
        "responses": {
            "200": _response("请求成功"),
            "400": _response("请求参数校验失败", "ErrorResponse"),
            "500": _response("服务内部错误", "ErrorResponse"),
        },
    }
    if admin:
        operation["security"] = [{"cookieAuth": []}]
        operation["responses"]["401"] = _response("管理员未登录", "ErrorResponse")
    return operation


def _response(description: str, schema_name: str | None = None) -> dict[str, Any]:
    schema = {"type": "object"} if not schema_name else {"$ref": f"#/components/schemas/{schema_name}"}
    return {"description": description, "content": _json_schema(schema)}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_ref(schema_name: str) -> dict[str, Any]:
    return _json_schema({"$ref": f"#/components/schemas/{schema_name}"})


def _json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": schema}}


def _body_ref(schema_name: str) -> dict[str, Any]:
    return {"required": True, "content": _json_ref(schema_name)}


def _admin_get(summary: str, parameters: list[dict[str, Any]] | None = None, tag: str = "Admin") -> dict[str, Any]:
    return {
        "get": {
            "tags": [tag],
            "summary": summary,
            "security": [{"cookieAuth": []}],
            "parameters": parameters or [],
            "responses": {
                "200": {"description": "请求成功", "content": _json_schema({"type": "object"})},
                "401": {"description": "管理员未登录", "content": _json_ref("ErrorResponse")},
            },
        }
    }


def _admin_post(summary: str, parameters: list[dict[str, Any]] | None = None, request_body: str | None = None, tag: str = "Admin") -> dict[str, Any]:
    operation: dict[str, Any] = {
        "tags": [tag],
        "summary": summary,
        "security": [{"cookieAuth": []}],
        "parameters": parameters or [],
        "responses": {
            "200": {"description": "请求成功", "content": _json_schema({"type": "object"})},
            "401": {"description": "管理员未登录", "content": _json_ref("ErrorResponse")},
        },
    }
    if request_body:
        operation["requestBody"] = _body_ref(request_body)
    return {"post": operation}


def _admin_patch(summary: str, parameters: list[dict[str, Any]] | None = None, request_body: str | None = None, tag: str = "Admin") -> dict[str, Any]:
    operation: dict[str, Any] = {
        "tags": [tag],
        "summary": summary,
        "security": [{"cookieAuth": []}],
        "parameters": parameters or [],
        "responses": {
            "200": {"description": "请求成功", "content": _json_schema({"type": "object"})},
            "401": {"description": "管理员未登录", "content": _json_ref("ErrorResponse")},
        },
    }
    if request_body:
        operation["requestBody"] = _body_ref(request_body)
    return {"patch": operation}


def _admin_delete(summary: str, parameters: list[dict[str, Any]] | None = None, tag: str = "Admin") -> dict[str, Any]:
    return {
        "delete": {
            "tags": [tag],
            "summary": summary,
            "security": [{"cookieAuth": []}],
            "parameters": parameters or [],
            "responses": {
                "200": {"description": "删除成功", "content": _json_schema({"type": "object"})},
                "401": {"description": "管理员未登录", "content": _json_ref("ErrorResponse")},
                "404": {"description": "记录不存在", "content": _json_ref("ErrorResponse")},
                "409": {"description": "记录状态不允许删除", "content": _json_ref("ErrorResponse")},
            },
        }
    }


def _query(name: str, schema_type: str, description: str) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "schema": {"type": schema_type}, "description": description}


def _path(name: str, schema_type: str, description: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": schema_type}, "description": description}
