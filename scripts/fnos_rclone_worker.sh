#!/bin/sh

# 飞牛影视入库 rclone 搬运脚本
# 用途：
#   1. 扫描夸克自动转存目录。
#   2. 在独立 Worker 内直接执行 rclone，把文件搬运到移动云盘任务暂存目录。
#   3. 新流程把完成事件交给 Organizer；兼容模式仍可按旧配置刷新媒体库。
#   4. 可选回调本系统，便于后续任务中心自动更新。
#
# 说明：
#   Worker 镜像内置 rclone，所有命令都在 Worker 内直接执行，不需要 Docker Socket。
#
# 使用：
#   chmod +x scripts/fnos_rclone_worker.sh
#   ./scripts/fnos_rclone_worker.sh
#
# 定时任务示例：
#   */10 * * * * /volume1/docker/fnos-media-import/scripts/fnos_rclone_worker.sh >> /volume1/docker/fnos-media-import/logs/rclone-worker.log 2>&1

set -eu

SCRIPT_VERSION="v0.9"
STOP_REQUESTED=0

RCLONE_CONFIG_PATH="${RCLONE_CONFIG_PATH:-/config/rclone/rclone.conf}"
RCLONE_CACHE_DIR="${RCLONE_CACHE_DIR:-/cache}"
REMOTE_NAME="${REMOTE_NAME:-MP}"
LOCAL_TEMP="${LOCAL_TEMP:-/temp/fnos-media-import}"
BUFFER_SIZE="${BUFFER_SIZE:-128M}"
DOWNLOAD_MULTI_THREAD="${DOWNLOAD_MULTI_THREAD:-16}"
UPLOAD_MULTI_THREAD="${UPLOAD_MULTI_THREAD:-1}"
MAX_RETRIES="${MAX_RETRIES:-3}"
DOWNLOAD_MAX_RETRIES="${DOWNLOAD_MAX_RETRIES:-$MAX_RETRIES}"
UPLOAD_MAX_RETRIES="${UPLOAD_MAX_RETRIES:-1}"
RETRY_DELAY="${RETRY_DELAY:-5}"
RCLONE_DOWNLOAD_RETRIES="${RCLONE_DOWNLOAD_RETRIES:-2}"
RCLONE_UPLOAD_RETRIES="${RCLONE_UPLOAD_RETRIES:-1}"
RCLONE_LOW_LEVEL_RETRIES="${RCLONE_LOW_LEVEL_RETRIES:-3}"
RCLONE_TIMEOUT="${RCLONE_TIMEOUT:-10m}"
RCLONE_CONNECT_TIMEOUT="${RCLONE_CONNECT_TIMEOUT:-30s}"
RCLONE_REPLACE_SIZE_MISMATCH="${RCLONE_REPLACE_SIZE_MISMATCH:-true}"
RCLONE_SOURCE_SETTLE_SECONDS="${RCLONE_SOURCE_SETTLE_SECONDS:-30}"
RCLONE_MANUAL_SOURCE_SETTLE_SECONDS="${RCLONE_MANUAL_SOURCE_SETTLE_SECONDS:-0}"
RCLONE_SOURCE_SETTLE_ROUNDS="${RCLONE_SOURCE_SETTLE_ROUNDS:-2}"
RCLONE_SOURCE_APPEAR_WAIT_SECONDS="${RCLONE_SOURCE_APPEAR_WAIT_SECONDS:-180}"
RCLONE_SOURCE_APPEAR_POLL_SECONDS="${RCLONE_SOURCE_APPEAR_POLL_SECONDS:-15}"
RCLONE_REFRESH_IN_WORKER="${RCLONE_REFRESH_IN_WORKER:-false}"
RCLONE_TEMP_RETENTION_HOURS="${RCLONE_TEMP_RETENTION_HOURS:-72}"
RCLONE_TEMP_CLEANUP_TIMEOUT="${RCLONE_TEMP_CLEANUP_TIMEOUT:-20}"
RCLONE_UPLOAD_MAX_DURATION="${RCLONE_UPLOAD_MAX_DURATION:-3h}"
RCLONE_DOWNLOAD_MAX_DURATION="${RCLONE_DOWNLOAD_MAX_DURATION:-3h}"
RCLONE_UPLOAD_STALL_TIMEOUT="${RCLONE_UPLOAD_STALL_TIMEOUT:-600}"
RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT="${RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT:-120}"
RCLONE_UPLOAD_PENDING_VERIFY_SECONDS="${RCLONE_UPLOAD_PENDING_VERIFY_SECONDS:-600}"
RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS="${RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS:-30}"
RCLONE_UPLOAD_PENDING_TTL="${RCLONE_UPLOAD_PENDING_TTL:-7200}"
RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL="${RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL:-true}"
RCLONE_VERIFY_SIZE_RETRIES="${RCLONE_VERIFY_SIZE_RETRIES:-6}"
RCLONE_VERIFY_SIZE_DELAY="${RCLONE_VERIFY_SIZE_DELAY:-5}"
RCLONE_LOCAL_OVERSIZE_BYTES="${RCLONE_LOCAL_OVERSIZE_BYTES:-10485760}"
RCLONE_UPLOAD_BACKEND="${RCLONE_UPLOAD_BACKEND:-cmcc_api}"
CMCC_UPLOAD_MODE="${CMCC_UPLOAD_MODE:-rapid_first}"
CMCC_UPLOAD_RENAME_MODE="${CMCC_UPLOAD_RENAME_MODE:-auto_rename}"
RCLONE_ROOT_FILE_FALLBACK_DIR="${RCLONE_ROOT_FILE_FALLBACK_DIR:-_待整理}"

PYTHON_BIN="${PYTHON_BIN:-python}"
APP_CALLBACK_URL="${APP_CALLBACK_URL:-}"
RCLONE_RUN_ID="${RCLONE_RUN_ID:-}"
RCLONE_TRIGGER_REASON="${RCLONE_TRIGGER_REASON:-}"
RCLONE_ONLY_CATEGORY="${RCLONE_ONLY_CATEGORY:-}"
RCLONE_ONLY_FILE="${RCLONE_ONLY_FILE:-}"
RCLONE_ONLY_JOB_DIR="${RCLONE_ONLY_JOB_DIR:-}"
RCLONE_RETRY_EVENT_ID="${RCLONE_RETRY_EVENT_ID:-}"
RCLONE_STAGING_ENABLED="${RCLONE_STAGING_ENABLED:-false}"

LOCK_FILE="${LOCK_FILE:-/tmp/fnos_media_import_rclone.lock}"
LOCK_DIR="${LOCK_FILE}.d"

RCLONE_SRC_MOVIE_DIR="${RCLONE_SRC_MOVIE_DIR:-离线下载/电影}"
RCLONE_SRC_TV_DIR="${RCLONE_SRC_TV_DIR:-离线下载/电视剧}"
RCLONE_SRC_ANIME_DIR="${RCLONE_SRC_ANIME_DIR:-离线下载/动漫}"
RCLONE_SRC_VARIETY_DIR="${RCLONE_SRC_VARIETY_DIR:-离线下载/综艺}"
RCLONE_SRC_OTHER_DIR="${RCLONE_SRC_OTHER_DIR:-离线下载/其他}"

RCLONE_DST_MOVIE_DIR="${RCLONE_DST_MOVIE_DIR:-移动云盘A/电影}"
RCLONE_DST_TV_DIR="${RCLONE_DST_TV_DIR:-移动云盘A/电视剧}"
RCLONE_DST_ANIME_DIR="${RCLONE_DST_ANIME_DIR:-移动云盘A/动漫}"
RCLONE_DST_VARIETY_DIR="${RCLONE_DST_VARIETY_DIR:-移动云盘A/综艺}"
RCLONE_DST_OTHER_DIR="${RCLONE_DST_OTHER_DIR:-移动云盘A/其他}"

# rclone + cmcc_api 是“先下载到本地 temp，再用 CMCC 官方上传接口写入目标目录”的链路。
# 139 官方直转的 CLOUD139_TARGET_*_FOLDER_ID 只属于分享转存目标，不能作为
# rclone 上传父目录兜底，否则 Quark/UC 追更会被写到 139 直转目录里。
CMCC_TARGET_MOVIE_PARENT_FILE_ID="${CMCC_TARGET_MOVIE_PARENT_FILE_ID:-}"
CMCC_TARGET_TV_PARENT_FILE_ID="${CMCC_TARGET_TV_PARENT_FILE_ID:-}"
CMCC_TARGET_ANIME_PARENT_FILE_ID="${CMCC_TARGET_ANIME_PARENT_FILE_ID:-}"
CMCC_TARGET_VARIETY_PARENT_FILE_ID="${CMCC_TARGET_VARIETY_PARENT_FILE_ID:-}"
CMCC_TARGET_OTHER_PARENT_FILE_ID="${CMCC_TARGET_OTHER_PARENT_FILE_ID:-}"

CMCC_TARGET_MOVIE_PARENT_PATH="${CMCC_TARGET_MOVIE_PARENT_PATH:-}"
CMCC_TARGET_TV_PARENT_PATH="${CMCC_TARGET_TV_PARENT_PATH:-}"
CMCC_TARGET_ANIME_PARENT_PATH="${CMCC_TARGET_ANIME_PARENT_PATH:-}"
CMCC_TARGET_VARIETY_PARENT_PATH="${CMCC_TARGET_VARIETY_PARENT_PATH:-}"
CMCC_TARGET_OTHER_PARENT_PATH="${CMCC_TARGET_OTHER_PARENT_PATH:-}"

EXCLUDE_PATTERNS='.!qB
.partial
.parts
.temp
.tmp
.crdownload'

# 远端任务清单在固化 manifest 前必须忽略仍可能被下载器继续写入的文件。
# 这里只按文件名后缀匹配，避免把名称中间包含 "part" 的正常资源误排除。
INCOMPLETE_FILE_SUFFIXES='.!qB
.partial
.part
.parts
.tmp
.temp
.download
.downloading
.crdownload
.aria2
.incomplete
.unfinished
.filepart'

# 格式：任务名|夸克源目录|移动云目标目录|飞牛库名
JOBS="离线电影|${RCLONE_SRC_MOVIE_DIR}|${RCLONE_DST_MOVIE_DIR}|电影
离线剧集|${RCLONE_SRC_TV_DIR}|${RCLONE_DST_TV_DIR}|电视剧
离线动漫|${RCLONE_SRC_ANIME_DIR}|${RCLONE_DST_ANIME_DIR}|动漫
离线综艺|${RCLONE_SRC_VARIETY_DIR}|${RCLONE_DST_VARIETY_DIR}|综艺
离线其他|${RCLONE_SRC_OTHER_DIR}|${RCLONE_DST_OTHER_DIR}|其他"

log() {
  # 日志必须写 stderr：部分函数通过 command substitution 捕获 stdout 作为文件列表。
  # 如果日志写 stdout，会被当成文件名去搬运。
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

request_stop() {
  STOP_REQUESTED=1
  log "收到停止请求，准备安全退出 rclone 搬运脚本"
}

should_stop() {
  [ "${STOP_REQUESTED:-0}" = "1" ]
}

is_interrupted_rc() {
  case "${1:-}" in
    130|143) return 0 ;;
    *) return 1 ;;
  esac
}

stop_if_requested() {
  if should_stop; then
    log "停止请求已生效，退出当前搬运任务"
    exit 143
  fi
}

sleep_or_stop() {
  seconds="$1"
  should_stop && return 143
  sleep "$seconds" || true
  should_stop && return 143
  return 0
}

is_manual_trigger() {
  reason="$(printf '%s' "${RCLONE_TRIGGER_REASON:-}" | tr '[:upper:]' '[:lower:]')"
  case "$reason" in
    *manual*) return 0 ;;
    *) return 1 ;;
  esac
}

trap 'request_stop' INT TERM

rclone_cmd() {
  rclone "$@" --config "$RCLONE_CONFIG_PATH" --cache-dir "$RCLONE_CACHE_DIR"
}

container_exec() {
  "$@"
}

container_exec_limited() {
  seconds="$1"
  shift
  case "$seconds" in
    ''|*[!0-9]*) seconds=0 ;;
  esac
  if [ "$seconds" -gt 0 ] && command -v timeout >/dev/null 2>&1; then
    timeout "${seconds}s" "$@"
    return $?
  fi
  if [ "$seconds" -gt 0 ]; then
    CLEANUP_TIMEOUT="$seconds" "$PYTHON_BIN" - "$@" <<'PY'
import os
import subprocess
import sys

try:
    completed = subprocess.run(sys.argv[1:], timeout=int(os.environ.get("CLEANUP_TIMEOUT", "0") or "0"))
    sys.exit(completed.returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
PY
    return $?
  fi
  "$@"
}

ensure_rclone_available() {
  if rclone version >/dev/null 2>&1; then
    return 0
  fi
  log "Worker 内的 rclone 命令不可用"
  exit 1
}

remote_path() {
  path="$1"
  printf '%s:%s' "$REMOTE_NAME" "$path"
}

safe_mkdir() {
  container_exec mkdir -p "$1"
}

ensure_remote_dir() {
  remote="$1"
  if rclone_cmd lsf "$remote" --max-depth 1 --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >/dev/null 2>&1; then
    return 0
  fi

  log "远端目录不存在或不可列出，尝试创建：${remote}"
  if ! rclone_cmd mkdir "$remote" >/dev/null 2>&1; then
    log "远端目录创建失败：${remote}"
    return 1
  fi

  if ! rclone_cmd lsf "$remote" --max-depth 1 --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >/dev/null 2>&1; then
    log "远端目录创建后仍不可访问：${remote}"
    return 1
  fi
  log "远端目录已就绪：${remote}"
  return 0
}

cleanup_local_temp() {
  dir="$1"
  safe_mkdir "$dir"
  log "开始清理本地 temp 缓存：${dir}，单步超时 ${RCLONE_TEMP_CLEANUP_TIMEOUT}s"
  while IFS= read -r pattern; do
    [ -z "$pattern" ] && continue
    if ! container_exec_limited "$RCLONE_TEMP_CLEANUP_TIMEOUT" find "$dir" -type f -name "*${pattern}*" -delete 2>/dev/null; then
      log "本地 temp 临时文件清理超时或失败，已跳过该规则，不影响继续搬运：${dir} pattern=${pattern}"
    fi
  done <<EOF
$EXCLUDE_PATTERNS
EOF
  cleanup_stale_local_temp "$dir"
  if ! container_exec_limited "$RCLONE_TEMP_CLEANUP_TIMEOUT" find "$dir" -mindepth 1 -type d -empty -delete 2>/dev/null; then
    log "本地 temp 空目录清理超时或失败，已跳过，不影响继续搬运：${dir}"
  fi
  log "本地 temp 启动清理结束：${dir}"
}

cleanup_stale_local_temp() {
  dir="$1"
  hours="${RCLONE_TEMP_RETENTION_HOURS:-0}"
  case "$hours" in
    ''|*[!0-9]*) hours=0 ;;
  esac
  if [ "$hours" -le 0 ]; then
    return 0
  fi
  minutes=$((hours * 60))
  log "清理历史失败 temp 缓存：${dir}，保留 ${hours} 小时内文件，超时 ${RCLONE_TEMP_CLEANUP_TIMEOUT}s"
  if container_exec_limited "$RCLONE_TEMP_CLEANUP_TIMEOUT" find "$dir" -type f -mmin "+${minutes}" -delete 2>/dev/null; then
    log "历史失败 temp 缓存清理完成：${dir}"
  else
    log "历史失败 temp 缓存清理超时或失败，已跳过本轮清理，不影响继续搬运：${dir}"
  fi
}

retry() {
  name="$1"
  shift
  retry_count "$name" "$MAX_RETRIES" "$@"
}

retry_count() {
  name="$1"
  max_attempts="$2"
  shift 2
  attempt=1
  while [ "$attempt" -le "$max_attempts" ]; do
    stop_if_requested
    if "$@"; then
      return 0
    else
      rc=$?
    fi
    if should_stop; then
      log "${name} 已被停止请求中断，停止重试"
      return 143
    fi
    if is_interrupted_rc "$rc"; then
      log "${name} 已被停止请求中断，停止重试"
      return "$rc"
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      return 1
    fi
    log "${name} 失败，${RETRY_DELAY}s 后重试 ${attempt}/${max_attempts}"
    attempt=$((attempt + 1))
    sleep_or_stop "$RETRY_DELAY" || return 143
  done
  return 1
}

download_file() {
  source="$1"
  target="$2"
  if [ -n "$RCLONE_DOWNLOAD_MAX_DURATION" ] && [ "$RCLONE_DOWNLOAD_MAX_DURATION" != "0" ]; then
    rclone_cmd copyto "$source" "$target" \
      --transfers 1 \
      --multi-thread-streams "$DOWNLOAD_MULTI_THREAD" \
      --buffer-size "$BUFFER_SIZE" \
      --no-traverse \
      --retries "$RCLONE_DOWNLOAD_RETRIES" \
      --low-level-retries "$RCLONE_LOW_LEVEL_RETRIES" \
      --retries-sleep "${RETRY_DELAY}s" \
      --timeout "$RCLONE_TIMEOUT" \
      --contimeout "$RCLONE_CONNECT_TIMEOUT" \
      --max-duration "$RCLONE_DOWNLOAD_MAX_DURATION" \
      --stats 5s \
      --stats-log-level NOTICE
  else
    rclone_cmd copyto "$source" "$target" \
      --transfers 1 \
      --multi-thread-streams "$DOWNLOAD_MULTI_THREAD" \
      --buffer-size "$BUFFER_SIZE" \
      --no-traverse \
      --retries "$RCLONE_DOWNLOAD_RETRIES" \
      --low-level-retries "$RCLONE_LOW_LEVEL_RETRIES" \
      --retries-sleep "${RETRY_DELAY}s" \
      --timeout "$RCLONE_TIMEOUT" \
      --contimeout "$RCLONE_CONNECT_TIMEOUT" \
      --stats 5s \
      --stats-log-level NOTICE
  fi
}

upload_file() {
  source="$1"
  target_file="$2"
  RCLONE_UPLOAD_SOURCE="$source" \
  RCLONE_UPLOAD_TARGET="$target_file" \
  RCLONE_CONFIG_PATH="$RCLONE_CONFIG_PATH" \
  RCLONE_CACHE_DIR="$RCLONE_CACHE_DIR" \
  RCLONE_UPLOAD_MULTI_THREAD_VALUE="$UPLOAD_MULTI_THREAD" \
  RCLONE_BUFFER_SIZE_VALUE="$BUFFER_SIZE" \
  RCLONE_UPLOAD_RETRIES_VALUE="$RCLONE_UPLOAD_RETRIES" \
  RCLONE_LOW_LEVEL_RETRIES_VALUE="$RCLONE_LOW_LEVEL_RETRIES" \
  RCLONE_RETRY_DELAY_VALUE="$RETRY_DELAY" \
  RCLONE_TIMEOUT_VALUE="$RCLONE_TIMEOUT" \
  RCLONE_CONNECT_TIMEOUT_VALUE="$RCLONE_CONNECT_TIMEOUT" \
  RCLONE_UPLOAD_MAX_DURATION_VALUE="$RCLONE_UPLOAD_MAX_DURATION" \
  RCLONE_UPLOAD_STALL_TIMEOUT_VALUE="$RCLONE_UPLOAD_STALL_TIMEOUT" \
  RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT_VALUE="$RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT" \
  "$PYTHON_BIN" - <<'PY'
import os
import re
import signal
import subprocess
import sys
import time


def env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def emit_guard(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


UNIT_MAP = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}
SIZE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*/\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*,\s*([0-9]{1,3})%"
)
COUNT_RE = re.compile(r"Transferred:\s+([0-9]+)\s*/\s*([0-9]+),\s*([0-9]{1,3})%")


def to_bytes(number: str, unit: str) -> int:
    return int(float(number) * UNIT_MAP.get(unit, 1))


source = os.environ["RCLONE_UPLOAD_SOURCE"]
target = os.environ["RCLONE_UPLOAD_TARGET"]
max_duration = str(os.environ.get("RCLONE_UPLOAD_MAX_DURATION_VALUE", "") or "").strip()
complete_stall_timeout = env_int("RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT_VALUE", 120)
stall_timeout = env_int("RCLONE_UPLOAD_STALL_TIMEOUT_VALUE", 600)

rclone_args = [
    "copyto",
    source,
    target,
    "--transfers",
    "1",
    "--checkers",
    "1",
    "--multi-thread-streams",
    str(os.environ.get("RCLONE_UPLOAD_MULTI_THREAD_VALUE", "1")),
    "--buffer-size",
    str(os.environ.get("RCLONE_BUFFER_SIZE_VALUE", "128M")),
    "--no-traverse",
    "--retries",
    str(os.environ.get("RCLONE_UPLOAD_RETRIES_VALUE", "1")),
    "--low-level-retries",
    str(os.environ.get("RCLONE_LOW_LEVEL_RETRIES_VALUE", "3")),
    "--retries-sleep",
    f"{os.environ.get('RCLONE_RETRY_DELAY_VALUE', '5')}s",
    "--timeout",
    str(os.environ.get("RCLONE_TIMEOUT_VALUE", "10m")),
    "--contimeout",
    str(os.environ.get("RCLONE_CONNECT_TIMEOUT_VALUE", "30s")),
]
if max_duration and max_duration != "0":
    rclone_args.extend(["--max-duration", max_duration])
rclone_args.extend(["--stats", "5s", "--stats-log-level", "NOTICE"])

cmd = [
    "rclone",
    *rclone_args,
    "--config",
    os.environ.get("RCLONE_CONFIG_PATH", "/config/rclone/rclone.conf"),
    "--cache-dir",
    os.environ.get("RCLONE_CACHE_DIR", "/cache"),
]

proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    bufsize=1,
)

last_progress_at = time.monotonic()
last_bytes = None
complete_since = None
completed_file_count = False
guard_reason = ""


def kill_upload_process(signal: str) -> None:
    if signal == "TERM":
        proc.terminate()
    else:
        proc.kill()


def handle_stop(signum, _frame) -> None:
    emit_guard(f"上传监控收到停止信号 {signum}，正在终止 rclone")
    try:
        kill_upload_process("TERM")
    finally:
        sys.exit(128 + int(signum))


signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)


try:
    emit_guard(f"上传监控已启动：pid={proc.pid}，100%卡住熔断={complete_stall_timeout}s，无进展熔断={stall_timeout}s")
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        now = time.monotonic()

        count_match = COUNT_RE.search(line)
        if count_match:
            done_count = int(count_match.group(1))
            total_count = int(count_match.group(2))
            count_percent = int(count_match.group(3))
            if total_count > 0 and done_count >= total_count and count_percent >= 100:
                completed_file_count = True
                complete_since = None
                last_progress_at = now

        size_match = SIZE_RE.search(line)
        if size_match:
            done_bytes = to_bytes(size_match.group(1), size_match.group(2))
            total_bytes = to_bytes(size_match.group(3), size_match.group(4))
            percent = int(size_match.group(5))

            if last_bytes is None or done_bytes > last_bytes:
                last_bytes = done_bytes
                last_progress_at = now

            if total_bytes > 0 and done_bytes >= total_bytes and percent >= 100 and not completed_file_count:
                if complete_since is None:
                    complete_since = now
            elif percent < 100:
                complete_since = None

        if complete_since is not None and complete_stall_timeout > 0 and now - complete_since >= complete_stall_timeout:
            guard_reason = f"rclone 上传已显示 100% 但文件完成数仍未确认，超过 {complete_stall_timeout}s，触发熔断"
            break
        if last_bytes is not None and stall_timeout > 0 and now - last_progress_at >= stall_timeout:
            guard_reason = f"rclone 上传长时间无字节进展，超过 {stall_timeout}s，触发熔断"
            break

    if guard_reason:
        emit_guard(f"上传熔断：{guard_reason}；将终止当前 rclone，随后由脚本校验云端文件大小决定成功/失败")
        try:
            kill_upload_process("TERM")
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            emit_guard("上传熔断：TERM 未结束，改用 KILL 强制终止")
            kill_upload_process("KILL")
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(88)

    sys.exit(proc.wait())
except KeyboardInterrupt:
    kill_upload_process("TERM")
    raise
except Exception as exc:
    emit_guard(f"上传监控异常：{exc}")
    try:
        kill_upload_process("TERM")
    except Exception:
        pass
    sys.exit(89)
PY
}

local_file_size() {
  file="$1"
  container_exec stat -c '%s' "$file" 2>/dev/null || true
}

download_marker_path() {
  printf '%s.fnos-download-complete' "$1"
}

mark_download_complete() {
  local_file="$1"
  expected_size="$2"
  [ -n "$expected_size" ] || return 0
  marker="$(download_marker_path "$local_file")"
  container_exec sh -c 'printf "%s\n" "$1" > "$2"' sh "$expected_size" "$marker" >/dev/null 2>&1 || true
}

clear_download_complete() {
  local_file="$1"
  marker="$(download_marker_path "$local_file")"
  container_exec rm -f "$marker" >/dev/null 2>&1 || true
}

download_complete_marker_matches() {
  local_file="$1"
  expected_size="$2"
  local_size="${3:-}"
  marker="$(download_marker_path "$local_file")"
  marker_size="$(container_exec cat "$marker" 2>/dev/null | sed -n '1p' | tr -d '\r' || true)"
  [ -n "$marker_size" ] || return 1
  if [ -n "$expected_size" ]; then
    [ -n "$local_size" ] && [ "$marker_size" = "$expected_size" ] && [ "$local_size" = "$expected_size" ]
    return $?
  fi
  [ -n "$local_size" ] && [ "$marker_size" = "$local_size" ]
}

remote_file_size() {
  remote="$1"
  rclone_cmd lsl "$remote" --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" 2>/dev/null | sed -n '1s/^ *\([0-9][0-9]*\).*/\1/p' || true
}

line_count() {
  text="$1"
  if [ -z "$text" ]; then
    printf '0'
    return 0
  fi
  printf '%s\n' "$text" | sed '/^$/d' | wc -l | tr -d ' '
}

list_remote_file_snapshots() {
  remote="$1"
  snapshot_json="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-source-snapshot.XXXXXX")" || return 1
  if [ "$RCLONE_STAGING_ENABLED" = "true" ] && [ -n "$RCLONE_ONLY_JOB_DIR" ]; then
    # 任务级暂存必须从列目录开始就只观察当前 job-N。
    # 否则其他任务的文件会干扰出现等待、稳定性判定和完整性 manifest。
    if ! rclone_cmd lsjson "$remote" -R --files-only --include "/${RCLONE_ONLY_JOB_DIR}/**" --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >"$snapshot_json"; then
      rm -f "$snapshot_json"
      return 1
    fi
  elif ! rclone_cmd lsjson "$remote" -R --files-only --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >"$snapshot_json"; then
    rm -f "$snapshot_json"
    return 1
  fi

  if "$PYTHON_BIN" - "$snapshot_json" <<'PY_REMOTE_SNAPSHOT'
import json
import sys


snapshot_path = sys.argv[1]

try:
    with open(snapshot_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("rclone lsjson output must be a JSON list")

    records = []
    for item in payload:
        if not isinstance(item, dict) or item.get("IsDir"):
            continue
        raw_path = item.get("Path")
        if not isinstance(raw_path, str):
            raise ValueError("rclone lsjson file entry is missing Path")
        relative_path = raw_path.lstrip("/")
        if not relative_path:
            continue
        if "Size" not in item or "ModTime" not in item:
            raise ValueError(f"rclone lsjson metadata is incomplete for {relative_path!r}")
        records.append([relative_path, int(item["Size"]), str(item["ModTime"])])

    records.sort(key=lambda record: record[0])
    for record in records:
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
except Exception as exc:  # noqa: BLE001
    print(f"rclone source snapshot parse failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY_REMOTE_SNAPSHOT
  then
    snapshot_rc=0
  else
    snapshot_rc=$?
  fi
  rm -f "$snapshot_json"
  return "$snapshot_rc"
}

emit_snapshot_paths() {
  snapshot_content="$1"
  snapshot_file="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-stable-paths.XXXXXX")" || return 1
  printf '%s\n' "$snapshot_content" >"$snapshot_file"
  if RCLONE_INCOMPLETE_FILE_SUFFIXES="$INCOMPLETE_FILE_SUFFIXES" "$PYTHON_BIN" - "$snapshot_file" <<'PY_REMOTE_PATHS'
import json
import os
import sys


suffixes = tuple(
    suffix.strip().casefold()
    for suffix in os.environ.get("RCLONE_INCOMPLETE_FILE_SUFFIXES", "").splitlines()
    if suffix.strip()
)

with open(sys.argv[1], encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        record = json.loads(line)
        if not isinstance(record, list) or len(record) != 3:
            raise ValueError("invalid rclone source snapshot record")
        relative_path = record[0]
        if not isinstance(relative_path, str) or not relative_path or relative_path.startswith("/"):
            raise ValueError("rclone source snapshot path must be relative")
        if suffixes and relative_path.casefold().endswith(suffixes):
            continue
        print(relative_path)
PY_REMOTE_PATHS
  then
    paths_rc=0
  else
    paths_rc=$?
  fi
  rm -f "$snapshot_file"
  return "$paths_rc"
}

stable_file_list() {
  remote="$1"
  job_name="$2"
  settle_seconds="${RCLONE_SOURCE_SETTLE_SECONDS:-0}"
  configured_settle_seconds="$settle_seconds"
  settle_rounds="${RCLONE_SOURCE_SETTLE_ROUNDS:-1}"
  manual_skip_settle=0
  scoped_staging=0
  if [ "$RCLONE_STAGING_ENABLED" = "true" ] && [ -n "$RCLONE_ONLY_JOB_DIR" ]; then
    scoped_staging=1
  fi
  case "$settle_seconds" in
    ''|*[!0-9]*) settle_seconds=0 ;;
  esac
  case "$configured_settle_seconds" in
    ''|*[!0-9]*) configured_settle_seconds="$settle_seconds" ;;
  esac
  if is_manual_trigger && [ -z "$RCLONE_ONLY_FILE" ] && [ "$scoped_staging" -ne 1 ]; then
    manual_settle_seconds="${RCLONE_MANUAL_SOURCE_SETTLE_SECONDS:-0}"
    case "$manual_settle_seconds" in
      ''|*[!0-9]*) manual_settle_seconds=0 ;;
    esac
    if [ "$settle_seconds" -gt 0 ] && [ "$manual_settle_seconds" -le 0 ]; then
      manual_skip_settle=1
    fi
    settle_seconds="$manual_settle_seconds"
  fi
  case "$settle_rounds" in
    ''|*[!0-9]*) settle_rounds=1 ;;
  esac
  if [ "$scoped_staging" -eq 1 ] && [ "$settle_seconds" -le 0 ]; then
    settle_seconds=30
    log "任务级暂存不允许跳过源目录稳定性确认，本轮使用 30s 安全等待"
  fi
  log "开始读取源目录文件列表：${job_name} ${remote}"
  if ! first="$(list_remote_file_snapshots "$remote")"; then
    log "源目录文件列表读取失败：${job_name}；本轮拒绝按空目录继续"
    return 1
  fi
  if [ -z "$first" ]; then
    appear_wait="${RCLONE_SOURCE_APPEAR_WAIT_SECONDS:-0}"
    appear_poll="${RCLONE_SOURCE_APPEAR_POLL_SECONDS:-15}"
    case "$appear_wait" in
      ''|*[!0-9]*) appear_wait=0 ;;
    esac
    case "$appear_poll" in
      ''|*[!0-9]*) appear_poll=15 ;;
    esac
    [ "$appear_poll" -gt 0 ] || appear_poll=15
    if [ -n "$RCLONE_ONLY_CATEGORY" ] && [ -z "$RCLONE_ONLY_FILE" ] && [ "$appear_wait" -gt 0 ]; then
      waited=0
      while [ "$waited" -lt "$appear_wait" ]; do
        stop_if_requested
        log "源目录暂未发现文件：${job_name}，等待 ${appear_poll}s 后复查（已等 ${waited}/${appear_wait}s）"
        sleep_or_stop "$appear_poll" || exit 143
        waited=$((waited + appear_poll))
        if ! first="$(list_remote_file_snapshots "$remote")"; then
          log "等待文件出现时源目录列表读取失败：${job_name}；本轮停止"
          return 1
        fi
        [ -n "$first" ] && break
      done
    fi
  fi
  if [ -z "$first" ]; then
    log "源目录文件列表为空：${job_name}"
    printf ''
    return 0
  fi
  log "源目录初始文件数：${job_name} $(line_count "$first")"
  skip_settle=0
  if [ -n "$RCLONE_ONLY_FILE" ] && [ "$scoped_staging" -ne 1 ]; then
    skip_settle=1
  fi
  if [ "$skip_settle" -eq 1 ] || [ "$settle_seconds" -le 0 ]; then
    if [ "$manual_skip_settle" -eq 1 ]; then
      log "源目录稳定性兜底：${job_name} 手动触发，跳过 ${configured_settle_seconds}s 等待"
    fi
    if ! emit_snapshot_paths "$first"; then
      log "源目录稳定快照无法还原为相对路径：${job_name}"
      return 1
    fi
    return 0
  fi

  round=1
  while [ "$round" -le "$settle_rounds" ]; do
    stop_if_requested
    first_count="$(line_count "$first")"
    log "源目录稳定性兜底：${job_name} 当前 ${first_count} 个文件，等待 ${settle_seconds}s 后复查"
    sleep_or_stop "$settle_seconds" || exit 143
    if ! second="$(list_remote_file_snapshots "$remote")"; then
      log "源目录稳定性复查失败：${job_name}；本轮拒绝按空目录继续"
      return 1
    fi
    second_count="$(line_count "$second")"
    if [ "$first" = "$second" ]; then
      log "源目录稳定性兜底通过：${job_name} 路径、大小和修改时间均稳定，文件数 ${second_count}"
      if ! emit_snapshot_paths "$second"; then
        log "源目录稳定快照无法还原为相对路径：${job_name}"
        return 1
      fi
      return 0
    fi
    log "源目录路径、大小或修改时间仍在变化：${job_name} ${first_count} -> ${second_count}，继续等待"
    first="$second"
    round=$((round + 1))
  done

  if [ "$scoped_staging" -eq 1 ]; then
    log "源目录在最大稳定性轮次内仍在变化，拒绝固化半批 manifest：${job_name} 当前文件数 $(line_count "$first")"
    return 1
  fi
  log "源目录稳定性兜底达到最大轮次，按最新列表继续：${job_name} 文件数 $(line_count "$first")"
  if ! emit_snapshot_paths "$first"; then
    log "源目录稳定快照无法还原为相对路径：${job_name}"
    return 1
  fi
}

verify_remote_size() {
  remote="$1"
  expected_size="$2"
  actual_size="$(remote_file_size "$remote")"
  [ -n "$expected_size" ] && [ "$actual_size" = "$expected_size" ]
}

wait_remote_size_match() {
  remote="$1"
  expected_size="$2"
  label="${3:-$remote}"
  retries="${RCLONE_VERIFY_SIZE_RETRIES:-6}"
  delay="${RCLONE_VERIFY_SIZE_DELAY:-5}"
  case "$retries" in
    ''|*[!0-9]*) retries=1 ;;
  esac
  case "$delay" in
    ''|*[!0-9]*) delay=1 ;;
  esac
  [ "$retries" -gt 0 ] || retries=1

  attempt=1
  while [ "$attempt" -le "$retries" ]; do
    stop_if_requested
    actual_size="$(remote_file_size "$remote")"
    if [ -n "$expected_size" ] && [ "$actual_size" = "$expected_size" ]; then
      [ "$attempt" -gt 1 ] && log "远端大小确认通过：${label} size=${actual_size}"
      return 0
    fi
    log "远端大小确认未通过：${label} source=${expected_size:-unknown} target=${actual_size:-none}，第 ${attempt}/${retries} 次"
    attempt=$((attempt + 1))
    [ "$attempt" -le "$retries" ] && sleep_or_stop "$delay" || true
    stop_if_requested
  done
  return 1
}

wait_remote_size_match_for() {
  remote="$1"
  expected_size="$2"
  label="$3"
  max_wait="${4:-0}"
  poll_seconds="${5:-30}"
  case "$max_wait" in
    ''|*[!0-9]*) max_wait=0 ;;
  esac
  case "$poll_seconds" in
    ''|*[!0-9]*) poll_seconds=30 ;;
  esac
  [ "$poll_seconds" -gt 0 ] || poll_seconds=30

  waited=0
  while [ "$waited" -le "$max_wait" ]; do
    stop_if_requested
    actual_size="$(remote_file_size "$remote")"
    if [ -n "$expected_size" ] && [ "$actual_size" = "$expected_size" ]; then
      log "上传提交兜底确认通过：${label} remote_size=${actual_size}"
      return 0
    fi
    log "上传提交仍未确认，等待云端落盘：${label} source=${expected_size:-unknown} target=${actual_size:-none} waited=${waited}/${max_wait}s"
    [ "$waited" -ge "$max_wait" ] && break
    sleep_or_stop "$poll_seconds" || return 143
    waited=$((waited + poll_seconds))
  done
  return 1
}

mark_upload_pending() {
  marker="$1"
  remote="$2"
  expected_size="$3"
  now_ts="$(date +%s)"
  container_exec sh -c 'printf "%s\n%s\n%s\n" "$1" "$2" "$3" > "$4"' sh "$remote" "$expected_size" "$now_ts" "$marker" >/dev/null 2>&1 || true
}

clear_upload_pending() {
  marker="$1"
  container_exec rm -f "$marker" >/dev/null 2>&1 || true
}

upload_pending_recent() {
  marker="$1"
  ttl="${RCLONE_UPLOAD_PENDING_TTL:-7200}"
  case "$ttl" in
    ''|*[!0-9]*) ttl=7200 ;;
  esac
  marker_mtime="$(container_exec stat -c '%Y' "$marker" 2>/dev/null || true)"
  case "$marker_mtime" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac
  now_ts="$(date +%s)"
  age=$((now_ts - marker_mtime))
  if [ "$age" -lt "$ttl" ]; then
    return 0
  fi
  log "上传待确认标记已过期，允许重新校验/重传：${marker} age=${age}s ttl=${ttl}s"
  clear_upload_pending "$marker"
  return 1
}

local_oversize_detected() {
  local_size="$1"
  source_size="$2"
  threshold="${RCLONE_LOCAL_OVERSIZE_BYTES:-10485760}"
  case "$local_size" in
    ''|*[!0-9]*) return 1 ;;
  esac
  case "$source_size" in
    ''|*[!0-9]*) return 1 ;;
  esac
  case "$threshold" in
    ''|*[!0-9]*) threshold=10485760 ;;
  esac
  limit=$((source_size + threshold))
  [ "$local_size" -gt "$limit" ]
}

delete_target_partial() {
  remote="$1"
  reason="$2"
  if [ "$RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL" != "true" ]; then
    log "上传熔断：保留目标残留文件，原因=${reason}，路径=${remote}"
    return 0
  fi
  log "上传熔断：删除目标残留文件，原因=${reason}，路径=${remote}"
  rclone_cmd deletefile "$remote" >/dev/null 2>&1 || {
    log "上传熔断：目标残留删除失败，为避免重复膨胀停止当前文件重试：${remote}"
    return 1
  }
  return 0
}

upload_with_circuit_breaker() {
  local_file="$1"
  dst_remote_file="$2"
  expected_size="$3"
  display_name="$4"
  reused_local_cache="${5:-0}"
  attempt=1
  max_attempts="$UPLOAD_MAX_RETRIES"
  case "$max_attempts" in
    ''|*[!0-9]*) max_attempts=1 ;;
  esac
  [ "$max_attempts" -gt 0 ] || max_attempts=1
  pending_marker="${local_file}.fnos-upload-pending"

  while [ "$attempt" -le "$max_attempts" ]; do
    stop_if_requested
    if upload_pending_recent "$pending_marker"; then
      log "发现上传提交待确认标记，先校验云端，不立即重新上传：${display_name}"
      if wait_remote_size_match_for "$dst_remote_file" "$expected_size" "$display_name" "$RCLONE_UPLOAD_PENDING_VERIFY_SECONDS" "$RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS"; then
        clear_upload_pending "$pending_marker"
        return 0
      fi
      log "上传提交仍未确认，本轮跳过重传，保留源端与本地缓存等待下次复查：${display_name}"
      return 2
    fi

    if [ "$attempt" -eq 1 ] && [ "$reused_local_cache" = "1" ]; then
      log "检测到本地完整缓存来自上次运行，可能是上次 100% 后云端仍在提交；先复查云端，避免直接重传：${display_name}"
      if wait_remote_size_match_for "$dst_remote_file" "$expected_size" "$display_name" "$RCLONE_UPLOAD_PENDING_VERIFY_SECONDS" "$RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS"; then
        clear_upload_pending "$pending_marker"
        return 0
      fi
      log "上次缓存对应的云端文件仍未确认，超过等待窗口后才允许重新上传：${display_name}"
    fi

    before_size="$(remote_file_size "$dst_remote_file")"
    if [ -n "$before_size" ] && [ "$before_size" != "$expected_size" ]; then
      if ! delete_target_partial "$dst_remote_file" "上传前发现目标大小不一致 source=${expected_size} target=${before_size}"; then
        return 1
      fi
    fi

    log "上传文件：${display_name}，第 ${attempt}/${max_attempts} 次，大小=${expected_size:-unknown}"
    upload_exit=0
    if upload_file "$local_file" "$dst_remote_file"; then
      if wait_remote_size_match "$dst_remote_file" "$expected_size" "$display_name"; then
        clear_upload_pending "$pending_marker"
        return 0
      fi
      after_size="$(remote_file_size "$dst_remote_file")"
      log "上传命令成功但远端大小校验不一致：${display_name} source=${expected_size:-unknown} target=${after_size:-unknown}"
    else
      upload_exit=$?
      if [ "$upload_exit" -eq 88 ]; then
        mark_upload_pending "$pending_marker" "$dst_remote_file" "$expected_size"
        log "上传 100% 后 rclone 未返回完成确认，已写入待确认标记并进入云端落盘复查：${display_name}"
        if wait_remote_size_match_for "$dst_remote_file" "$expected_size" "$display_name" "$RCLONE_UPLOAD_PENDING_VERIFY_SECONDS" "$RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS"; then
          clear_upload_pending "$pending_marker"
          return 0
        fi
        log "上传 100% 后仍未确认云端落盘，本轮不删除目标、不重传，等待后续复查：${display_name}"
        return 2
      fi
      after_size="$(remote_file_size "$dst_remote_file")"
      if wait_remote_size_match "$dst_remote_file" "$expected_size" "$display_name"; then
        log "上传命令返回失败，但远端文件大小已匹配，按成功处理：${display_name}"
        clear_upload_pending "$pending_marker"
        return 0
      fi
      log "上传命令失败：${display_name}，远端大小=${after_size:-none}"
    fi

    after_size="$(remote_file_size "$dst_remote_file")"
    if [ -n "$after_size" ] && [ "$after_size" != "$expected_size" ]; then
      if ! delete_target_partial "$dst_remote_file" "上传失败后远端大小不一致 source=${expected_size:-unknown} target=${after_size}"; then
        return 1
      fi
    fi

    attempt=$((attempt + 1))
    if [ "$attempt" -le "$max_attempts" ]; then
      log "上传重试等待 ${RETRY_DELAY}s：${display_name}"
      sleep_or_stop "$RETRY_DELAY" || return 143
    fi
  done
  return 1
}

cmcc_parent_id_for_category() {
  # 历史 CMCC_TARGET_*_PARENT_FILE_ID 无法在本地证明仍指向当前目标路径；
  # 一旦它指向旧 /离线下载 或 139 直转目录，上传会直接串到错误目录。
  # 因此这里不再使用环境里的 parent id，统一让 CMCC API 按目标路径 ensure_path。
  printf '%s' ""
}

cmcc_parent_path_for_category() {
  cmcc_category_key="$1"
  cmcc_fallback_dst_dir="$2"
  cmcc_fallback_category_label="${3:-}"
  case "$cmcc_category_key" in
    movie) cmcc_value="$CMCC_TARGET_MOVIE_PARENT_PATH" ;;
    tv) cmcc_value="$CMCC_TARGET_TV_PARENT_PATH" ;;
    anime) cmcc_value="$CMCC_TARGET_ANIME_PARENT_PATH" ;;
    variety) cmcc_value="$CMCC_TARGET_VARIETY_PARENT_PATH" ;;
    other) cmcc_value="$CMCC_TARGET_OTHER_PARENT_PATH" ;;
    *) cmcc_value="" ;;
  esac
  cmcc_normalized_value="$(printf '%s' "$cmcc_value" | sed 's#\\#/#g; s#^/*##; s#/*$##')"
  cmcc_normalized_dst="$(printf '%s' "$cmcc_fallback_dst_dir" | sed 's#\\#/#g; s#^/*##; s#/*$##')"
  # CMCC_TARGET_* 是移动云官方 API 的真实目录；RCLONE_DST_* 只属于
  # WebDAV 回退后端。两者不能混用，否则 webDAV/电影会被写成移动云/电影。
  if [ -n "$cmcc_value" ]; then
    printf '%s' "$cmcc_value"
    return 0
  fi
  if [ -n "$cmcc_normalized_dst" ]; then
    printf '%s' "$cmcc_fallback_dst_dir"
    return 0
  fi
  cmcc_fallback_name="$cmcc_fallback_category_label"
  if [ -z "$cmcc_fallback_name" ]; then
    cmcc_fallback_name="$(basename "$(printf '%s' "$cmcc_fallback_dst_dir" | sed 's#\\#/#g; s#/*$##')")"
  fi
  printf '/%s' "$(printf '%s' "$cmcc_fallback_name" | sed 's#^/*##; s#/*$##')"
}

normalize_job_dir() {
  printf '%s' "$1" | sed 's#\\#/#g; s#^[^/][^/]*:[/]*##; s#^/*##; s#/*$##' | tr '[:upper:]' '[:lower:]'
}

dedupe_adjacent_path_segments() {
  input_path="$1"
  result_path=""
  previous_part=""
  old_ifs="$IFS"
  IFS='/'
  set -- $input_path
  IFS="$old_ifs"
  for path_part do
    [ -n "$path_part" ] || continue
    if [ "$path_part" = "$previous_part" ]; then
      continue
    fi
    if [ -n "$result_path" ]; then
      result_path="${result_path}/${path_part}"
    else
      result_path="$path_part"
    fi
    previous_part="$path_part"
  done
  printf '%s' "${result_path:-.}"
}

job_dirs_overlap() {
  left="$(normalize_job_dir "$1")"
  right="$(normalize_job_dir "$2")"
  [ -n "$left" ] && [ -n "$right" ] || return 1
  [ "$left" = "$right" ] && return 0
  case "$left" in
    "$right"/*) return 0 ;;
  esac
  case "$right" in
    "$left"/*) return 0 ;;
  esac
  return 1
}

cmcc_json_summary() {
  cmcc_result_file="$1"
  "$PYTHON_BIN" - "$cmcc_result_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    print(f"invalid_json: {exc}")
    raise SystemExit(0)

items = []
for key in ("success", "status", "mode", "file_id", "upload_id", "target_name", "size", "sha256", "message"):
    value = payload.get(key)
    if value not in (None, ""):
        text = str(value)
        if key == "sha256" and len(text) > 16:
            text = text[:16] + "..."
        items.append(f"{key}={text}")
raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
create = raw.get("create") if isinstance(raw.get("create"), dict) else {}
if create:
    keys = ",".join(sorted(str(key) for key in create.keys()))
    if keys:
        items.append(f"create_keys={keys[:240]}")
    for msg_key in ("message", "msg", "desc", "error", "errorMsg", "code"):
        value = create.get(msg_key)
        if value not in (None, ""):
            items.append(f"create_{msg_key}={str(value)[:240]}")
            break
print(", ".join(items))
PY
}

cmcc_drain_new_stderr() {
  cmcc_stderr_file="$1"
  cmcc_last_line="${2:-0}"
  [ -f "$cmcc_stderr_file" ] || {
    printf '%s' "$cmcc_last_line"
    return 0
  }
  cmcc_total_lines="$(wc -l <"$cmcc_stderr_file" | tr -d ' ' 2>/dev/null || printf '0')"
  case "$cmcc_total_lines" in
    ''|*[!0-9]*) cmcc_total_lines=0 ;;
  esac
  case "$cmcc_last_line" in
    ''|*[!0-9]*) cmcc_last_line=0 ;;
  esac
  if [ "$cmcc_total_lines" -gt "$cmcc_last_line" ]; then
    cmcc_start_line=$((cmcc_last_line + 1))
    sed -n "${cmcc_start_line},${cmcc_total_lines}p" "$cmcc_stderr_file" | while IFS= read -r line; do
      [ -z "$line" ] && continue
      case "$line" in
        CMCC\ API\ *)
          log "$line"
          ;;
        *)
          log "CMCC API upload stderr: ${line}"
          ;;
      esac
    done
  fi
  printf '%s' "$cmcc_total_lines"
}

cmcc_verify_upload_result() {
  cmcc_result_file="$1"
  cmcc_expected_name="$2"
  cmcc_expected_size="$3"
  "$PYTHON_BIN" - "$cmcc_result_file" "$cmcc_expected_name" "$cmcc_expected_size" <<'PY'
import json
import sys
from pathlib import Path

result_file = Path(sys.argv[1])
expected_name = sys.argv[2]
expected_size = sys.argv[3]
try:
    payload = json.loads(result_file.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    print(f"结果 JSON 无法读取：{exc}", file=sys.stderr)
    raise SystemExit(1)

actual_name = str(payload.get("target_name") or "")
actual_size = str(payload.get("size") or "")
errors = []
if expected_name and actual_name != expected_name:
    errors.append(f"目标文件名串线：expected={expected_name}, actual={actual_name}")
if expected_size and actual_size != expected_size:
    errors.append(f"文件大小串线：expected={expected_size}, actual={actual_size}")
if errors:
    print("；".join(errors), file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
PY
}

cmcc_upload_file() {
  cmcc_local_file="$1"
  cmcc_target_parent_file_id="$2"
  cmcc_target_parent_path="$3"
  cmcc_target_name="$4"
  cmcc_target_relative_dir="$5"
  cmcc_category_key="$6"
  cmcc_display_name="$7"

  cmcc_result_file="${cmcc_local_file}.fnos-cmcc-result.json"
  cmcc_stderr_file="${cmcc_local_file}.fnos-cmcc-result.stderr"
  cmcc_rc_file="${cmcc_local_file}.fnos-cmcc-result.rc"
  cmcc_expected_size="$(local_file_size "$cmcc_local_file")"
  rm -f "$cmcc_result_file" "$cmcc_stderr_file" "$cmcc_rc_file" >/dev/null 2>&1 || true

  stop_if_requested
  log "CMCC API upload start: ${cmcc_display_name}, local=$(basename "$cmcc_local_file"), target=${cmcc_target_name:-$(basename "$cmcc_local_file")}, mode=${CMCC_UPLOAD_MODE}, parent_id=${cmcc_target_parent_file_id:-none}, parent_path=${cmcc_target_parent_path:-none}, relative_dir=${cmcc_target_relative_dir:-.}"
  set +e
  (
    PYTHONPATH=".:${PYTHONPATH:-}" "$PYTHON_BIN" -m fnos_media_import.cmcc.cli upload \
      --local-file "$cmcc_local_file" \
      --target-parent-file-id "$cmcc_target_parent_file_id" \
      --target-parent-path "$cmcc_target_parent_path" \
      --target-relative-dir "$cmcc_target_relative_dir" \
      --target-name "$cmcc_target_name" \
      --category "$cmcc_category_key" \
      --mode "$CMCC_UPLOAD_MODE" \
      --rename-mode "$CMCC_UPLOAD_RENAME_MODE" \
      >"$cmcc_result_file" 2>"$cmcc_stderr_file"
    printf '%s\n' "$?" >"$cmcc_rc_file"
  ) &
  cmcc_pid=$!
  cmcc_rc=""
  cmcc_last_stderr_line=0
  while [ ! -f "$cmcc_rc_file" ]; do
    cmcc_last_stderr_line="$(cmcc_drain_new_stderr "$cmcc_stderr_file" "$cmcc_last_stderr_line")"
    if should_stop; then
      log "CMCC API upload stop requested, terminating pid=${cmcc_pid}: ${cmcc_display_name}"
      kill "$cmcc_pid" >/dev/null 2>&1 || true
      wait "$cmcc_pid" >/dev/null 2>&1 || true
      cmcc_rc=143
      break
    fi
    sleep 2
  done
  if [ -z "$cmcc_rc" ]; then
    wait "$cmcc_pid" >/dev/null 2>&1 || true
    cmcc_rc="$(sed -n '1p' "$cmcc_rc_file" 2>/dev/null | tr -d '\r' || true)"
    case "$cmcc_rc" in
      ''|*[!0-9]*) cmcc_rc=1 ;;
    esac
  fi
  cmcc_last_stderr_line="$(cmcc_drain_new_stderr "$cmcc_stderr_file" "$cmcc_last_stderr_line")"
  set -e
  if [ -s "$cmcc_result_file" ]; then
    cmcc_summary="$(cmcc_json_summary "$cmcc_result_file" 2>/dev/null || true)"
    log "CMCC API upload result: ${cmcc_display_name}, exit=${cmcc_rc}, ${cmcc_summary}"
    if [ "$cmcc_rc" -eq 0 ]; then
      if ! cmcc_verify_message="$(cmcc_verify_upload_result "$cmcc_result_file" "$cmcc_target_name" "$cmcc_expected_size" 2>&1)"; then
        log "CMCC API upload result mismatch，拒绝删除源端：${cmcc_display_name}；${cmcc_verify_message}"
        return 1
      fi
    else
      log "CMCC API detailed result json: ${cmcc_result_file}"
    fi
  else
    log "CMCC API upload result: ${cmcc_display_name}, exit=${cmcc_rc}, no result json"
  fi
  if should_stop && is_interrupted_rc "$cmcc_rc"; then
    log "CMCC API upload interrupted by stop request: ${cmcc_display_name}"
  fi
  return "$cmcc_rc"
}

finish_successful_file() {
  finished_source_remote_file="$1"
  finished_source_remote_dir="$2"
  finished_source_dirname="$3"
  finished_local_file="$4"

  log "单文件成功，立即清理本地缓存：${finished_source_remote_file}"
  container_exec rm -f \
    "$finished_local_file" \
    "$(download_marker_path "$finished_local_file")" \
    "${finished_local_file}.fnos-upload-pending" \
    "${finished_local_file}.fnos-cmcc-result.json" \
    "${finished_local_file}.fnos-cmcc-result.stderr" \
    "${finished_local_file}.fnos-cmcc-result.rc" \
    "${finished_local_file}.fnos-cmcc-upload.json" \
    >/dev/null 2>&1 || true
  source_delete_retries=3
  delete_attempt=1
  delete_ok=0
  while [ "$delete_attempt" -le "$source_delete_retries" ]; do
    if rclone_cmd deletefile "$finished_source_remote_file" >/dev/null 2>&1; then
      delete_ok=1
      log "源端文件已删除，避免下次重复下载：${finished_source_remote_file}"
      break
    fi
    log "源端文件删除失败，稍后重试 ${delete_attempt}/${source_delete_retries}：${finished_source_remote_file}"
    delete_attempt=$((delete_attempt + 1))
    sleep_or_stop 2 || return 143
  done
  if [ "$delete_ok" -ne 1 ]; then
    log "源端文件删除失败，已保留源端；下次可能再次扫描到该文件：${finished_source_remote_file}"
  fi
  [ "$finished_source_dirname" != "." ] && rclone_cmd rmdir "${finished_source_remote_dir}/${finished_source_dirname}" >/dev/null 2>&1 || true
}

fnos_refresh_local() {
  FNOS_REFRESH_LIBRARY="$1" FNOS_REFRESH_DIR_LIST="$2" PYTHONPATH=".:${PYTHONPATH:-}" "$PYTHON_BIN" - <<'PY'
import os
import sys

try:
    from fnos_media_import.config import load_config
    from fnos_media_import.media.fnos import FnosMediaRefresher

    config = load_config().raw
    result = FnosMediaRefresher(config.get("fnos", {})).refresh(
        os.environ.get("FNOS_REFRESH_LIBRARY", ""),
        dir_list=os.environ.get("FNOS_REFRESH_DIR_LIST", ""),
    )
    print(result.get("message") or result)
    sys.exit(0 if result.get("success") else 1)
except Exception as exc:  # noqa: BLE001
    print(exc, file=sys.stderr)
    sys.exit(1)
PY
}

http_callback_post() {
  CALLBACK_URL="$1" CALLBACK_RUN_ID="$2" CALLBACK_STATUS="$3" CALLBACK_CATEGORY="$4" CALLBACK_FILENAME="$5" CALLBACK_TARGET_PATH="$6" CALLBACK_SOURCE_PATH="$7" CALLBACK_MOVED_COUNT="${8:-}" CALLBACK_FAILED_COUNT="${9:-}" CALLBACK_MESSAGE="${10:-}" CALLBACK_NAMING_PLAN="${11:-}" CALLBACK_MANIFEST_FILE="${12:-}" "$PYTHON_BIN" - <<'PY'
import json
import os
import posixpath
import re
import sys
import urllib.request

url = os.environ["CALLBACK_URL"]
payload = {
    "run_id": os.environ.get("CALLBACK_RUN_ID", ""),
    "status": os.environ.get("CALLBACK_STATUS", ""),
    "category": os.environ.get("CALLBACK_CATEGORY", ""),
    "filename": os.environ.get("CALLBACK_FILENAME", ""),
    "source_path": os.environ.get("CALLBACK_SOURCE_PATH", ""),
    "target_path": os.environ.get("CALLBACK_TARGET_PATH", ""),
    "retry_event_id": os.environ.get("RCLONE_RETRY_EVENT_ID", ""),
    "message": f"rclone {os.environ.get('CALLBACK_STATUS', '')}: {os.environ.get('CALLBACK_FILENAME', '')}",
}
trigger_reason = os.environ.get("RCLONE_TRIGGER_REASON", "").strip()
payload["trigger_reason"] = trigger_reason
payload["require_job_match"] = trigger_reason.startswith(
    ("public_submit:", "update_", "trending_", "file_retry:")
)
job_dir_match = re.fullmatch(r"job-([1-9][0-9]{0,8})", os.environ.get("RCLONE_ONLY_JOB_DIR", "").strip())
if job_dir_match:
    payload["job_id"] = int(job_dir_match.group(1))
    payload["require_job_match"] = True
custom_message = os.environ.get("CALLBACK_MESSAGE", "")
if custom_message:
    payload["message"] = custom_message
naming_plan = os.environ.get("CALLBACK_NAMING_PLAN", "")
if naming_plan:
    try:
        payload["naming_plan"] = json.loads(naming_plan)
    except Exception:
        payload["naming_plan"] = {"raw": naming_plan}
for key, env_name in (("moved_count", "CALLBACK_MOVED_COUNT"), ("failed_count", "CALLBACK_FAILED_COUNT")):
    raw = os.environ.get(env_name, "")
    if raw != "":
        try:
            payload[key] = int(raw)
        except ValueError:
            payload[key] = raw
if payload.get("status") == "staging_manifest":
    manifest_file = os.environ.get("CALLBACK_MANIFEST_FILE", "").strip()
    source_root = str(payload.get("source_path") or "").strip().replace("\\", "/").rstrip("/")
    if not manifest_file or not source_root or not payload.get("job_id"):
        print("任务级 manifest 缺少文件、源路径或 job_id", file=sys.stderr)
        sys.exit(1)
    try:
        manifest_rows = open(manifest_file, encoding="utf-8").read().splitlines()
    except Exception as exc:  # noqa: BLE001
        print(f"任务级 manifest 读取失败：{exc}", file=sys.stderr)
        sys.exit(1)
    manifest_paths = []
    seen_paths = set()
    for raw_path in manifest_rows:
        relative_path = str(raw_path or "").strip().replace("\\", "/").lstrip("/")
        normalized_relative = posixpath.normpath(relative_path)
        if (
            not relative_path
            or normalized_relative in {"", ".", ".."}
            or normalized_relative.startswith("../")
        ):
            continue
        full_path = posixpath.normpath(f"{source_root}/{normalized_relative}")
        if full_path in seen_paths:
            continue
        seen_paths.add(full_path)
        manifest_paths.append(full_path)
    if not manifest_paths:
        print("任务级 manifest 没有可用文件", file=sys.stderr)
        sys.exit(1)
    payload["event_kind"] = "staging_manifest"
    payload["manifest_version"] = 1
    payload["manifest_paths"] = manifest_paths
    payload["expected_file_count"] = len(manifest_paths)
if payload.get("status") in {"category_done", "category_failed"}:
    payload["event_kind"] = "category_summary"
    payload["message"] = (
        f"rclone {payload.get('status')}: {payload.get('category')}; "
        f"moved={payload.get('moved_count', 0)}, failed={payload.get('failed_count', 0)}"
    )
data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
request = urllib.request.Request(
    url,
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=10) as response:
        response_body = response.read()
except Exception as exc:  # noqa: BLE001
    print(exc, file=sys.stderr)
    sys.exit(1)

try:
    response_payload = json.loads(response_body)
except Exception as exc:  # noqa: BLE001
    print(f"rclone 回调响应不是有效 JSON：{exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(response_payload, dict):
    print("rclone 回调响应必须是 JSON 对象", file=sys.stderr)
    sys.exit(1)
if response_payload.get("success") is not True:
    print(
        f"rclone 回调未获业务确认：success={response_payload.get('success')!r}",
        file=sys.stderr,
    )
    sys.exit(1)

expected_job_id = payload.get("job_id")
if expected_job_id and str(payload.get("status") or "").strip().lower() in {"done", "success", "skipped_existing"}:
    disposition = str(response_payload.get("disposition") or "").strip().lower()
    delete_source_allowed = response_payload.get("delete_source_allowed")
    if disposition not in {"accepted", "ignored_terminal"}:
        print(
            "rclone 完成回调未提供允许删除源端的有效 disposition："
            f"{disposition!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if delete_source_allowed is not True:
        print(
            "rclone 完成回调未明确确认 delete_source_allowed=true，"
            "为安全起见保留源端与本地缓存",
            file=sys.stderr,
        )
        sys.exit(1)
if expected_job_id:
    response_job_ids = []
    response_job = response_payload.get("job")
    if isinstance(response_job, dict) and "id" in response_job:
        response_job_ids.append(("job.id", response_job.get("id")))
    response_manifest = response_payload.get("manifest")
    if isinstance(response_manifest, dict) and "job_id" in response_manifest:
        response_job_ids.append(("manifest.job_id", response_manifest.get("job_id")))
    for response_field, response_job_id in response_job_ids:
        try:
            normalized_response_job_id = int(response_job_id)
        except (TypeError, ValueError):
            print(
                f"rclone 回调响应 {response_field} 无效：{response_job_id!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        if normalized_response_job_id != int(expected_job_id):
            print(
                "rclone 回调响应任务不匹配："
                f"request.job_id={expected_job_id}, "
                f"response.{response_field}={normalized_response_job_id}",
                file=sys.stderr,
            )
            sys.exit(1)
PY
}

refresh_fnos() {
  library="$1"
  dir_list="${2:-}"
  if [ -z "$library" ]; then
    return 0
  fi
  if [ -n "$dir_list" ]; then
    log "刷新飞牛媒体库：${library}，目录：${dir_list}"
  else
    log "刷新飞牛媒体库：${library}"
  fi
  fnos_refresh_local "$library" "$dir_list" >/dev/null 2>&1 || log "飞牛刷新失败：${library}"
}

callback_app() {
  callback_status="$1"
  callback_category="$2"
  callback_filename="$3"
  callback_target_path="$4"
  callback_source_path="$5"
  callback_moved_count="${6:-}"
  callback_failed_count="${7:-}"
  callback_message="${8:-}"
  callback_naming_plan="${9:-}"
  callback_manifest_file="${10:-}"
  if [ -z "$APP_CALLBACK_URL" ]; then
    return 0
  fi

  callback_attempt=1
  while [ "$callback_attempt" -le 3 ]; do
    if http_callback_post "$APP_CALLBACK_URL" "$RCLONE_RUN_ID" "$callback_status" "$callback_category" "$callback_filename" "$callback_target_path" "$callback_source_path" "$callback_moved_count" "$callback_failed_count" "$callback_message" "$callback_naming_plan" "$callback_manifest_file" >/dev/null; then
      return 0
    fi
    log "rclone callback failed: status=${callback_status}, category=${callback_category}, file=${callback_filename:-none}, attempt=${callback_attempt}/3"
    [ "$callback_attempt" -lt 3 ] && sleep "$callback_attempt"
    callback_attempt=$((callback_attempt + 1))
  done
  return 1
}

ack_staging_file_completion() {
  if [ "$RCLONE_STAGING_ENABLED" != "true" ]; then
    return 0
  fi
  if [ -z "$APP_CALLBACK_URL" ]; then
    log "任务级暂存缺少主程序回调地址，拒绝删除已上传文件的源端与本地缓存"
    return 1
  fi
  callback_app "$@"
}

persist_staging_manifest() {
  manifest_content="$1"
  manifest_category="$2"
  manifest_target_root="$3"
  manifest_source_root="$4"
  if [ "$RCLONE_STAGING_ENABLED" != "true" ] || [ -z "$RCLONE_ONLY_JOB_DIR" ]; then
    return 0
  fi
  if [ -z "$APP_CALLBACK_URL" ]; then
    log "任务级暂存缺少回调地址，拒绝在未持久化 manifest 时搬运"
    return 1
  fi
  manifest_file="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-manifest.XXXXXX")" || return 1
  printf '%s\n' "$manifest_content" >"$manifest_file"
  manifest_count="$(line_count "$manifest_content")"
  if callback_app "staging_manifest" "$manifest_category" "" "$manifest_target_root" "$manifest_source_root" "$manifest_count" "" "rclone 已固化任务级源文件清单：${manifest_count} 个文件" "" "$manifest_file"; then
    rm -f "$manifest_file"
    return 0
  fi
  rm -f "$manifest_file"
  return 1
}

notify_legacy_file_completion() {
  if [ "$RCLONE_STAGING_ENABLED" = "true" ]; then
    return 0
  fi
  callback_app "$@" || true
}

needs_root_resource_wrapper() {
  category_key="$1"
  dirname_value="$2"
  [ "$dirname_value" = "." ] || return 1
  case "$category_key" in
    tv|anime|variety) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_root_resource_dir() {
  category_label="$1"
  category_key="$2"
  filename="$3"
  source_path="$4"
  target_path="$5"
  if [ -n "$APP_CALLBACK_URL" ]; then
    resolved="$(
      CALLBACK_URL="$APP_CALLBACK_URL" \
      CALLBACK_RUN_ID="$RCLONE_RUN_ID" \
      CALLBACK_CATEGORY="$category_label" \
      CALLBACK_FILENAME="$filename" \
      CALLBACK_SOURCE_PATH="$source_path" \
      CALLBACK_TARGET_PATH="$target_path" \
      "$PYTHON_BIN" - <<'PY'
import json
import os
import sys
import urllib.request

payload = {
    "run_id": os.environ.get("CALLBACK_RUN_ID", ""),
    "status": "resolve_folder",
    "category": os.environ.get("CALLBACK_CATEGORY", ""),
    "filename": os.environ.get("CALLBACK_FILENAME", ""),
    "source_path": os.environ.get("CALLBACK_SOURCE_PATH", ""),
    "target_path": os.environ.get("CALLBACK_TARGET_PATH", ""),
    "message": "resolve resource folder",
}
try:
    request = urllib.request.Request(
        os.environ["CALLBACK_URL"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    value = str(data.get("resource_dir") or "").strip().replace("\\", "/")
    value = "/".join(part.strip(" .") for part in value.split("/") if part.strip(" ."))
    print(value)
except Exception:
    print("")
PY
    )"
    if [ -n "$resolved" ]; then
      printf '%s' "$resolved"
      return 0
    fi
  fi

  FALLBACK_ROOT="$RCLONE_ROOT_FILE_FALLBACK_DIR" FALLBACK_FILENAME="$filename" "$PYTHON_BIN" - <<'PY'
import os
import re
from pathlib import PurePosixPath

root = str(os.environ.get("FALLBACK_ROOT") or "_待整理").strip().strip("/").replace("\\", "/") or "_待整理"
name = PurePosixPath(str(os.environ.get("FALLBACK_FILENAME") or "未命名资源")).stem
name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', " ", name)
name = re.sub(r"\s+", " ", name).strip(" .-_") or "未命名资源"
print(f"{root}/{name}")
PY
}

build_standard_naming_plan_exports() {
  category_label="$1"
  category_key="$2"
  filename="$3"
  source_path="$4"
  target_path="$5"
  target_file="$6"
  CALLBACK_URL="$APP_CALLBACK_URL" \
  CALLBACK_RUN_ID="$RCLONE_RUN_ID" \
  CALLBACK_CATEGORY="$category_label" \
  CALLBACK_CATEGORY_KEY="$category_key" \
  CALLBACK_FILENAME="$filename" \
  CALLBACK_SOURCE_PATH="$source_path" \
  CALLBACK_TARGET_PATH="$target_path" \
  CALLBACK_TARGET_FILE="$target_file" \
  PYTHONPATH=".:${PYTHONPATH:-}" "$PYTHON_BIN" - <<'PY'
import json
import os
import shlex
import urllib.request

from fnos_media_import.media_path_rules import build_standard_naming_plan


def callback_plan() -> dict:
    url = os.environ.get("CALLBACK_URL", "").strip()
    if not url:
        return {}
    payload = {
        "run_id": os.environ.get("CALLBACK_RUN_ID", ""),
        "status": "naming_plan",
        "category": os.environ.get("CALLBACK_CATEGORY", ""),
        "filename": os.environ.get("CALLBACK_FILENAME", ""),
        "source_path": os.environ.get("CALLBACK_SOURCE_PATH", ""),
        "target_path": os.environ.get("CALLBACK_TARGET_PATH", ""),
        "message": "build upload naming plan",
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    plan = data.get("naming_plan") if isinstance(data, dict) else {}
    return plan if isinstance(plan, dict) else {}


try:
    plan = callback_plan()
except Exception:
    plan = {}
if not plan:
    plan = build_standard_naming_plan(
        category_key=os.environ.get("CALLBACK_CATEGORY_KEY", ""),
        filename=os.environ.get("CALLBACK_FILENAME", ""),
        target_file=os.environ.get("CALLBACK_TARGET_FILE", ""),
        source_file=os.environ.get("CALLBACK_SOURCE_PATH", ""),
    )

target_dir = str(plan.get("target_relative_dir") or "").strip().replace("\\", "/").strip("/")
target_name = str(plan.get("target_name") or os.environ.get("CALLBACK_FILENAME", "")).strip()
target_file = str(plan.get("target_file") or "").strip().replace("\\", "/").strip("/")
if not target_file:
    target_file = f"{target_dir}/{target_name}".strip("/") if target_dir else target_name
plan["target_relative_dir"] = target_dir
plan["target_name"] = target_name
plan["target_file"] = target_file
plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
exports = {
    "PLAN_TARGET_DIRNAME": target_dir or ".",
    "PLAN_TARGET_NAME": target_name,
    "PLAN_TARGET_FILE": target_file,
    "PLAN_APPLIED": "true" if plan.get("applied") else "false",
    "PLAN_REASON": str(plan.get("reason") or ""),
    "PLAN_JSON": plan_json,
}
for key, value in exports.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
}

process_job() {
  stop_if_requested
  job_name="$1"
  src_dir="$2"
  dst_dir="$3"
  library="$4"
  category_key="${5:-}"

  effective_target_dir="$dst_dir"
  if [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ]; then
    effective_target_dir="$(cmcc_parent_path_for_category "$category_key" "$dst_dir" "$library")"
  fi
  source_remote="$(remote_path "$src_dir")"
  target_remote="$(remote_path "$dst_dir")"
  local_root="${LOCAL_TEMP}/${job_name}"
  moved_count=0
  failed_count=0

  log "扫描任务：${job_name} (${source_remote} -> ${effective_target_dir})"
  if job_dirs_overlap "$src_dir" "$effective_target_dir"; then
    log "目录映射配置错误，源端中转目录与目标目录重叠，拒绝真实搬运以避免上传回中转目录：${src_dir} -> ${effective_target_dir}"
    callback_app "category_failed" "$category_key" "" "$effective_target_dir" "$src_dir" "0" "1" "rclone 目录映射错误：源端中转目录与目标目录重叠"
    return 1
  fi
  if ! ensure_remote_dir "$source_remote"; then
    log "源目录不可用，跳过任务：${job_name}"
    return 1
  fi
  if [ "$RCLONE_UPLOAD_BACKEND" = "webdav" ]; then
    if ! ensure_remote_dir "$target_remote"; then
      log "目标目录不可用，跳过任务：${job_name}"
      return 1
    fi
  else
    log "upload backend=${RCLONE_UPLOAD_BACKEND}; rclone only downloads, CMCC API handles cloud upload"
  fi
  safe_mkdir "$local_root"
  cleanup_local_temp "$local_root"

  if ! file_list="$(stable_file_list "$source_remote" "$job_name")"; then
    log "源目录尚未稳定，本轮不生成 manifest、不处理也不删除任何文件：${job_name}"
    return 1
  fi
  if [ -z "$file_list" ]; then
    log "无待搬运文件：${job_name}"
    return 0
  fi
  if ! persist_staging_manifest "$file_list" "$category_key" "$effective_target_dir" "$src_dir"; then
    log "任务级源文件 manifest 未获得主程序确认，本轮不处理任何文件：${job_name}"
    return 1
  fi

  while IFS= read -r file; do
    stop_if_requested
    [ -z "$file" ] && continue
    case "$file" in
      \[20[0-9][0-9]-*)
        log "跳过疑似日志污染行，避免误当文件搬运：${file}"
        continue
        ;;
    esac

    filename="$(basename "$file")"
    dirname="$(dirname "$file")"
    if [ "$RCLONE_STAGING_ENABLED" = "true" ]; then
      staging_job_dir="${file%%/*}"
      case "$staging_job_dir" in
        job-*) staging_job_id="${staging_job_dir#job-}" ;;
        *)
          log "任务级暂存已启用，跳过不属于 job-<任务 ID> 目录的文件：${file}"
          continue
          ;;
      esac
      case "$staging_job_id" in
        ''|*[!0-9]*|0*)
          log "任务级暂存已启用，跳过无效 job 目录：${file}"
          continue
          ;;
      esac
      if [ "${#staging_job_id}" -gt 9 ]; then
        log "任务级暂存已启用，跳过超出范围的 job 目录：${file}"
        continue
      fi
      if [ -n "$RCLONE_ONLY_JOB_DIR" ] && [ "$staging_job_dir" != "$RCLONE_ONLY_JOB_DIR" ]; then
        continue
      fi
    fi
    if [ -n "$RCLONE_ONLY_FILE" ] && [ "$file" != "$RCLONE_ONLY_FILE" ] && [ "$filename" != "$RCLONE_ONLY_FILE" ]; then
      continue
    fi

    local_dir="${local_root}/${dirname}"
    [ "$dirname" = "." ] && local_dir="$local_root"
    local_file="${local_dir}/${filename}"
    source_remote_file="${source_remote}/${file}"
    source_callback_path="${src_dir}/${file}"
    target_dirname="$(dedupe_adjacent_path_segments "$dirname")"
    target_file="${target_dirname}/${filename}"
    [ "$target_dirname" = "." ] && target_file="$filename"
    if [ "$target_dirname" != "$dirname" ]; then
      log "检测到连续重复目录，上传时自动去重：${dirname} -> ${target_dirname}"
    fi
    upload_filename="$filename"
    if needs_root_resource_wrapper "$category_key" "$dirname"; then
      resolved_dir="$(resolve_root_resource_dir "$category_key" "$category_key" "$filename" "$source_callback_path" "${effective_target_dir}/${filename}")"
      if [ -n "$resolved_dir" ]; then
        target_dirname="$resolved_dir"
        target_file="${target_dirname}/${filename}"
        log "检测到剧集类根目录文件，自动加资源目录：${file} -> ${target_file}"
      fi
    fi
    target_callback_path="${effective_target_dir}/${target_file}"
    naming_plan_json=""
    # rclone 阶段只负责搬运并保留源端相对结构，最终影视标准命名统一交给
    # OpenList Organizer 在整批文件完成后处理。这里不能再把文件改成
    # Season/SxxEyy，否则目录噪音、广告、谐音标题会提前污染云端路径。
    dst_remote_dir="${target_remote}/${target_dirname}"
    [ "$target_dirname" = "." ] && dst_remote_dir="$target_remote"
    dst_remote_file="${dst_remote_dir}/${upload_filename}"
    target_callback_path="${effective_target_dir}/${target_file}"

    log "处理文件：${file}"
    callback_app "transferring" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "rclone 已开始处理文件：${file}" "$naming_plan_json"
    safe_mkdir "$local_dir"

    source_size="$(remote_file_size "$source_remote_file")"
    if [ "$RCLONE_UPLOAD_BACKEND" = "webdav" ]; then
      target_size="$(remote_file_size "$dst_remote_file")"
    else
      target_size=""
    fi
    if [ -n "$target_size" ]; then
      if [ -n "$source_size" ] && [ "$target_size" = "$source_size" ]; then
        if ! ack_staging_file_completion "skipped_existing" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"; then
          log "目标文件已存在，但完成回调未确认；保留源端与本地缓存等待重试：${file}"
          failed_count=$((failed_count + 1))
          continue
        fi
        log "目标同名文件大小一致，视为已搬运，删除源端重复文件：${file}"
        clear_upload_pending "${local_file}.fnos-upload-pending"
        clear_download_complete "$local_file"
        container_exec rm -f "$local_file" >/dev/null 2>&1 || true
        rclone_cmd deletefile "$source_remote_file" >/dev/null 2>&1 || true
        [ "$dirname" != "." ] && rclone_cmd rmdir "${source_remote}/${dirname}" >/dev/null 2>&1 || true
        notify_legacy_file_completion "skipped_existing" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
        continue
      fi

      if [ "$RCLONE_REPLACE_SIZE_MISMATCH" = "true" ]; then
        log "目标同名文件大小不一致，疑似上次失败残留，将删除后重传：${file} (source=${source_size:-unknown}, target=${target_size})"
        rclone_cmd deletefile "$dst_remote_file" >/dev/null 2>&1 || true
      else
        log "目标同名文件大小不一致，为避免误删已跳过：${file} (source=${source_size:-unknown}, target=${target_size})"
        callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        continue
      fi
    fi

    download_ok=0
    download_result=0
    reused_local_cache=0
    local_size="$(local_file_size "$local_file")"
    if local_oversize_detected "$local_size" "$source_size"; then
      message="上传异常：本地 temp 文件比源端大超过 ${RCLONE_LOCAL_OVERSIZE_BYTES} 字节，判定进入异常膨胀/死循环，已删除本地异常缓存：${file} local=${local_size} source=${source_size}"
      log "$message"
      clear_download_complete "$local_file"
      container_exec rm -f "$local_file" >/dev/null 2>&1 || true
      callback_app "upload_error" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
      failed_count=$((failed_count + 1))
      continue
    elif [ -n "$source_size" ] && [ "$local_size" = "$source_size" ]; then
      log "本地缓存文件大小一致，跳过重复下载：${file}"
      mark_download_complete "$local_file" "$source_size"
      download_ok=1
      reused_local_cache=1
    elif download_complete_marker_matches "$local_file" "$source_size" "$local_size"; then
      log "发现本地下载完成标记且缓存大小匹配，跳过重复下载：${file}"
      download_ok=1
      reused_local_cache=1
    else
      if [ -n "$local_size" ]; then
        log "本地缓存大小未确认或与源端不一致，将重新下载：${file} local=${local_size} source=${source_size:-unknown}"
      fi
      clear_download_complete "$local_file"
      if retry_count "下载 ${filename}" "$DOWNLOAD_MAX_RETRIES" download_file "$source_remote_file" "$local_file"; then
        download_ok=1
      else
        download_result=$?
        if should_stop; then
          message="下载被停止请求中断，保留源端和本地缓存：${file}"
          log "$message"
          callback_app "stopped" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
          return 143
        fi
        if is_interrupted_rc "$download_result"; then
          message="下载被停止请求中断，保留源端和本地缓存：${file}"
          log "$message"
          callback_app "stopped" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
          return "$download_result"
        fi
      fi
    fi

    if [ "$download_ok" -eq 1 ]; then
      stop_if_requested
      local_size="$(local_file_size "$local_file")"
      if [ -z "$local_size" ]; then
        log "本地文件大小读取失败，跳过上传：${file}"
        clear_download_complete "$local_file"
        callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        continue
      fi
      if [ -n "$source_size" ] && [ "$local_size" != "$source_size" ]; then
        message="下载后本地文件大小与源端不一致，判定缓存不完整，跳过上传等待下次重试：${file} local=${local_size} source=${source_size}"
        log "$message"
        clear_download_complete "$local_file"
        callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        continue
      fi
      if local_oversize_detected "$local_size" "$source_size"; then
        message="上传异常：下载后本地文件比源端大超过 ${RCLONE_LOCAL_OVERSIZE_BYTES} 字节，判定异常膨胀，已删除本地异常缓存：${file} local=${local_size} source=${source_size}"
        log "$message"
        clear_download_complete "$local_file"
        container_exec rm -f "$local_file" >/dev/null 2>&1 || true
        callback_app "upload_error" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        continue
      fi
      mark_download_complete "$local_file" "${source_size:-$local_size}"

      if [ "$RCLONE_UPLOAD_BACKEND" = "webdav" ]; then
        [ "$target_dirname" != "." ] && ensure_remote_dir "$dst_remote_dir" >/dev/null 2>&1 || true
      fi

      upload_ok=0
      upload_result=0
      if [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ]; then
        current_file="$file"
        current_filename="$filename"
        current_local_file="$local_file"
        current_source_remote_file="$source_remote_file"
        current_source_remote="$source_remote"
        current_dirname="$dirname"
        current_target_callback_path="$target_callback_path"
        current_source_callback_path="$source_callback_path"
        current_naming_plan_json="$naming_plan_json"
        current_local_basename="$(basename "$current_local_file")"
        if [ "$current_local_basename" != "$current_filename" ]; then
          message="rclone 内部文件绑定异常，拒绝上传并保留源端：source=${current_filename}, local=${current_local_basename}, local_path=${current_local_file}"
          log "$message"
          callback_app "upload_error" "$category_key" "$current_filename" "$current_target_callback_path" "$current_source_callback_path" "" "" "$message" "$current_naming_plan_json"
          failed_count=$((failed_count + 1))
          continue
        fi
        cmcc_parent_id="$(cmcc_parent_id_for_category "$category_key")"
        cmcc_parent_path="$effective_target_dir"
        if job_dirs_overlap "$src_dir" "$cmcc_parent_path"; then
          message="CMCC API 上传目标与源端中转目录重叠，拒绝上传以避免写回中转目录：source=${src_dir}, target=${cmcc_parent_path}"
          log "$message"
          callback_app "upload_error" "$category_key" "$current_filename" "$current_target_callback_path" "$current_source_callback_path" "" "" "$message" "$current_naming_plan_json"
          failed_count=$((failed_count + 1))
          continue
        fi
        cmcc_relative_dir="$target_dirname"
        [ "$cmcc_relative_dir" = "." ] && cmcc_relative_dir=""
        if cmcc_upload_file "$current_local_file" "$cmcc_parent_id" "$cmcc_parent_path" "$upload_filename" "$cmcc_relative_dir" "$category_key" "$current_file"; then
          upload_ok=1
        else
          upload_result=$?
        fi
      else
        if upload_with_circuit_breaker "$local_file" "$dst_remote_file" "$local_size" "$file" "$reused_local_cache"; then
          upload_ok=1
        else
          upload_result=$?
        fi
      fi
      if should_stop; then
        message="收到停止请求，上传被中断，保留源端和本地缓存：${file}"
        log "$message"
        callback_app "stopped" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        return 143
      fi
      if is_interrupted_rc "$upload_result"; then
        message="收到停止请求，上传被中断，保留源端和本地缓存：${file}"
        log "$message"
        callback_app "stopped" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        return "$upload_result"
      fi

      if [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ] && [ "$upload_ok" -eq 1 ]; then
        log "搬运完成：${current_file}"
        if ! ack_staging_file_completion "done" "$category_key" "$current_filename" "$current_target_callback_path" "$current_source_callback_path" "" "" "" "$current_naming_plan_json"; then
          log "文件已上传，但完成回调未确认；保留源端与本地缓存等待重试：${current_file}"
          failed_count=$((failed_count + 1))
          continue
        fi
        finish_successful_file "$current_source_remote_file" "$current_source_remote" "$current_dirname" "$current_local_file"
        moved_count=$((moved_count + 1))
        notify_legacy_file_completion "done" "$category_key" "$current_filename" "$current_target_callback_path" "$current_source_callback_path" "" "" "" "$current_naming_plan_json"
      elif [ "$upload_ok" -eq 1 ] && wait_remote_size_match "$dst_remote_file" "$local_size" "$file"; then
        log "搬运完成：${file}"
        if ! ack_staging_file_completion "done" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"; then
          log "文件已上传，但完成回调未确认；保留源端与本地缓存等待重试：${file}"
          failed_count=$((failed_count + 1))
          continue
        fi
        finish_successful_file "$source_remote_file" "$source_remote" "$dirname" "$local_file"
        moved_count=$((moved_count + 1))
        notify_legacy_file_completion "done" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
      elif [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ] && [ "$upload_result" -eq 12 ]; then
        message="CMCC API auth expired: ${file}; keep source and local cache"
        log "$message"
        callback_app "auth_expired" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        log "CMCC 认证已失效，停止本轮搬运，避免继续下载后续文件"
        return "$upload_result"
      elif [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ] && [ "$upload_result" -eq 11 ]; then
        message="CMCC API auth config error: ${file}; keep source and local cache"
        log "$message"
        callback_app "auth_config_error" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
        log "CMCC 请求头/认证配置不完整，停止本轮搬运，避免继续下载后续文件"
        return "$upload_result"
      elif [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ] && [ "$upload_result" -eq 20 ]; then
        message="CMCC rapid upload missed in rapid_only mode: ${file}; keep source and local cache"
        log "$message"
        callback_app "rapid_miss" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
      elif [ "$upload_result" -eq 2 ]; then
        message="上传提交待确认：${file} 已避免立即重传，保留源端和本地缓存等待云端落盘复查"
        log "$message"
        callback_app "upload_pending" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
      elif [ "$RCLONE_UPLOAD_BACKEND" = "cmcc_api" ]; then
        message="CMCC API 上传失败，保留源端和本地缓存等待下次重试：${file} exit=${upload_result}"
        log "$message"
        callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "$message" "$naming_plan_json"
        failed_count=$((failed_count + 1))
      else
        log "上传失败或远端大小校验不一致，保留源端和本地缓存等待下次重试：${file}"
        callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
        failed_count=$((failed_count + 1))
      fi
    else
      clear_download_complete "$local_file"
      log "下载失败，保留源端等待下次重试：${file}"
      callback_app "failed" "$category_key" "$filename" "$target_callback_path" "$source_callback_path" "" "" "" "$naming_plan_json"
      failed_count=$((failed_count + 1))
    fi
  done <<EOF
$file_list
EOF

  rclone_cmd rmdirs "$source_remote" --leave-root >/dev/null 2>&1 || true
  cleanup_local_temp "$local_root"

  if [ "$moved_count" -gt 0 ] && [ "$failed_count" -eq 0 ] && [ "$RCLONE_STAGING_ENABLED" != "true" ] && { [ "$RCLONE_REFRESH_IN_WORKER" = "true" ] || [ -z "$APP_CALLBACK_URL" ]; }; then
    refresh_fnos "$library" "$dst_dir"
  elif [ "$moved_count" -gt 0 ]; then
    if [ "$RCLONE_STAGING_ENABLED" = "true" ] && [ -z "$APP_CALLBACK_URL" ]; then
      log "分类搬运完成，但任务级暂存缺少主程序回调地址，Organizer 无法接管：${library}"
    else
      log "分类搬运完成，已通过回调移交 Organizer 整理：${library}"
    fi
  fi
  callback_app "$([ "$failed_count" -eq 0 ] && printf 'category_done' || printf 'category_failed')" "$category_key" "" "$effective_target_dir" "$src_dir" "$moved_count" "$failed_count"
  log "任务结束：${job_name}，搬运 ${moved_count} 个文件，失败 ${failed_count} 个文件"
  [ "$failed_count" -eq 0 ]
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
    return 0
  fi

  log "已有 rclone 搬运任务运行中，本次跳过"
  exit 0
}

main() {
  acquire_lock

  log "飞牛影视 rclone 搬运脚本启动 ${SCRIPT_VERSION}"
  log "upload backend: ${RCLONE_UPLOAD_BACKEND}, cmcc mode: ${CMCC_UPLOAD_MODE}"
  if [ "$RCLONE_STAGING_ENABLED" = "true" ] && [ -z "$RCLONE_ONLY_JOB_DIR" ]; then
    log "任务级暂存已启用，但未绑定具体 job 目录；拒绝按可变后台配置扫描全部任务"
    exit 2
  fi
  if [ -n "$RCLONE_ONLY_CATEGORY" ] || [ -n "$RCLONE_ONLY_FILE" ] || [ -n "$RCLONE_ONLY_JOB_DIR" ]; then
    log "搬运过滤：分类=${RCLONE_ONLY_CATEGORY:-不限}，任务目录=${RCLONE_ONLY_JOB_DIR:-不限}，文件=${RCLONE_ONLY_FILE:-不限}"
  fi
  ensure_rclone_available

  overall_failed=0
  job_index=0
  while IFS= read -r job; do
    stop_if_requested
    [ -z "$job" ] && continue
    job_index=$((job_index + 1))
    old_ifs="$IFS"
    IFS='|'
    set -- $job
    IFS="$old_ifs"
    case "$job_index" in
      1) category_key="movie" ;;
      2) category_key="tv" ;;
      3) category_key="anime" ;;
      4) category_key="variety" ;;
      5) category_key="other" ;;
      *) category_key="" ;;
    esac
    if [ -n "$RCLONE_ONLY_CATEGORY" ] && [ "$RCLONE_ONLY_CATEGORY" != "$category_key" ] && [ "$RCLONE_ONLY_CATEGORY" != "$1" ] && [ "$RCLONE_ONLY_CATEGORY" != "$4" ]; then
      continue
    fi
    set +e
    process_job "$1" "$2" "$3" "$4" "$category_key"
    job_rc=$?
    set -e
    if is_interrupted_rc "$job_rc"; then
      log "停止请求已生效，脚本不再处理后续分类"
      exit "$job_rc"
    fi
    if [ "$job_rc" -eq 11 ] || [ "$job_rc" -eq 12 ]; then
      log "CMCC 认证/请求头错误，脚本不再处理后续分类，exit=${job_rc}"
      exit "$job_rc"
    fi
    if [ "$job_rc" -ne 0 ]; then
      overall_failed=1
    fi
  done <<EOF
$JOBS
EOF

  stop_if_requested
  log "全部任务完成"
  exit "$overall_failed"
}

main "$@"
