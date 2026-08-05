"""项目常量。

中文文案集中保存在 UTF-8 源码中，避免脚本拼接时产生编码问题。
"""

APP_NAME = "飞牛影视统一入库维护系统"

CATEGORY_MOVIE = "movie"
CATEGORY_TV = "tv"
CATEGORY_ANIME = "anime"
CATEGORY_VARIETY = "variety"
CATEGORY_OTHER = "other"

CATEGORY_LABELS = {
    CATEGORY_MOVIE: "电影",
    CATEGORY_TV: "电视剧",
    CATEGORY_ANIME: "动漫",
    CATEGORY_VARIETY: "综艺",
    CATEGORY_OTHER: "其他",
}

SOURCE_QUARK = "quark"
SOURCE_CLOUD139 = "cloud139"
SOURCE_CLOUD189 = "cloud189"
SOURCE_MAGNET = "magnet"
SOURCE_TORRENT = "torrent"
SOURCE_ALIYUN = "aliyun"
SOURCE_BAIDU = "baidu"
SOURCE_UC = "uc"
SOURCE_UNKNOWN = "unknown"

ROUTE_QUARK_TO_MOBILE = "quark_to_mobile"
ROUTE_CLOUD139_DIRECT = "cloud139_direct"
ROUTE_CLOUD189_DIRECT = "cloud189_direct"
ROUTE_SIXPAN_OFFLINE = "sixpan_offline"
ROUTE_UNSUPPORTED = "unsupported"

JOB_CREATED = "created"
JOB_PROVIDER_SUBMITTING = "provider_submitting"
JOB_CHECKING = "checking"
JOB_SUBMITTED = "submitted"
JOB_WAITING_TRANSFER = "waiting_transfer"
JOB_TRANSFERRING = "transferring"
JOB_WAITING_OPENLIST = "waiting_openlist"
JOB_WAITING_ORGANIZER = "waiting_organizer"
JOB_ORGANIZING = "organizing"
JOB_CONFIRMING = "confirming"
JOB_REVIEW = "review"
JOB_REFRESHING = "refreshing"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_UNSUPPORTED = "unsupported"
JOB_CANCELLED = "cancelled"

JOB_STATUS_LABELS = {
    JOB_CREATED: "已创建",
    JOB_PROVIDER_SUBMITTING: "正在提交到网盘（结果待确认）",
    JOB_CHECKING: "正在检测",
    JOB_SUBMITTED: "已提交",
    JOB_WAITING_TRANSFER: "等待搬运",
    JOB_TRANSFERRING: "搬运中",
    JOB_WAITING_OPENLIST: "等待 OpenList 可见",
    JOB_WAITING_ORGANIZER: "等待整理",
    JOB_ORGANIZING: "整理中",
    JOB_CONFIRMING: "正在确认标准目录",
    JOB_REVIEW: "等待人工确认",
    JOB_REFRESHING: "刷新中",
    JOB_DONE: "已完成",
    JOB_FAILED: "失败",
    JOB_UNSUPPORTED: "暂不支持",
    JOB_CANCELLED: "已取消",
}

EVENT_INFO = "info"
EVENT_WARN = "warn"
EVENT_ERROR = "error"

RCLONE_RUN_RUNNING = "running"
RCLONE_RUN_SUCCESS = "success"
RCLONE_RUN_FAILED = "failed"

# rclone worker 回调状态（POST /api/callback/rclone 的 status 字段）。
# 这些值是对外契约：worker 脚本发送的字符串必须与之一致，改动需同步脚本。
CALLBACK_STATUS_RESOLVE_FOLDER = "resolve_folder"
CALLBACK_STATUS_NAMING_PLAN = "naming_plan"
CALLBACK_STATUS_STAGING_MANIFEST = "staging_manifest"
CALLBACK_STATUS_CATEGORY_DONE = "category_done"
CALLBACK_STATUS_CATEGORY_FAILED = "category_failed"
CALLBACK_STATUS_DONE = "done"
CALLBACK_STATUS_SUCCESS = "success"
CALLBACK_STATUS_SKIPPED_EXISTING = "skipped_existing"
CALLBACK_STATUS_TRANSFERRING = "transferring"
CALLBACK_STATUS_PROCESSING = "processing"
CALLBACK_STATUS_FAILED = "failed"
CALLBACK_STATUS_ERROR = "error"
CALLBACK_STATUS_UPLOAD_ERROR = "upload_error"
CALLBACK_STATUS_UPLOAD_EXCEPTION = "upload_exception"
CALLBACK_STATUS_AUTH_EXPIRED = "auth_expired"
CALLBACK_STATUS_AUTH_CONFIG_ERROR = "auth_config_error"
CALLBACK_STATUS_RAPID_MISS = "rapid_miss"
CALLBACK_STATUS_UPLOAD_PENDING = "upload_pending"
CALLBACK_STATUS_STOPPED = "stopped"

# 完成类回调状态：允许在 ACK 中放行删除源文件。
COMPLETION_CALLBACK_STATUSES = frozenset(
    {
        CALLBACK_STATUS_DONE,
        CALLBACK_STATUS_SUCCESS,
        CALLBACK_STATUS_SKIPPED_EXISTING,
    }
)

# 入库任务 completion.stage 值（写入 import_jobs.raw_data.completion.stage）。
COMPLETION_STAGE_WAITING_TRANSFER = "waiting_transfer"
COMPLETION_STAGE_WAITING_ORGANIZER = "waiting_organizer"
COMPLETION_STAGE_WAITING_OPENLIST = "waiting_openlist"
COMPLETION_STAGE_REVIEW = "review"
COMPLETION_STAGE_FAILED = "failed"
COMPLETION_STAGE_DONE = "done"
