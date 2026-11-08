#!/bin/sh

# V1 task-scoped rclone worker.
# Only an immutable rclone_copy descriptor may invoke this script. Global
# directory scans, config-selected routes, script-embedded provider upload
# backends and process-wide provider fallbacks were retired by the V1 cutover.

set -eu

SCRIPT_VERSION="v1.4-bounded-relay"
STOP_REQUESTED=0

RCLONE_CONFIG_PATH="${RCLONE_CONFIG_PATH:-/config/rclone/rclone.conf}"
RCLONE_CACHE_DIR="${RCLONE_CACHE_DIR:-/cache}"
BUFFER_SIZE="${BUFFER_SIZE:-128M}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_DELAY="${RETRY_DELAY:-5}"
RCLONE_UPLOAD_RETRIES="${RCLONE_UPLOAD_RETRIES:-1}"
RCLONE_LOW_LEVEL_RETRIES="${RCLONE_LOW_LEVEL_RETRIES:-3}"
RCLONE_TIMEOUT="${RCLONE_TIMEOUT:-10m}"
RCLONE_CONNECT_TIMEOUT="${RCLONE_CONNECT_TIMEOUT:-30s}"
RCLONE_LIST_MAX_DURATION="${RCLONE_LIST_MAX_DURATION:-10m}"
RCLONE_SOURCE_SETTLE_SECONDS="${RCLONE_SOURCE_SETTLE_SECONDS:-10}"
RCLONE_SOURCE_SETTLE_ROUNDS="${RCLONE_SOURCE_SETTLE_ROUNDS:-2}"
RCLONE_SOURCE_APPEAR_WAIT_SECONDS="${RCLONE_SOURCE_APPEAR_WAIT_SECONDS:-180}"
RCLONE_SOURCE_APPEAR_POLL_SECONDS="${RCLONE_SOURCE_APPEAR_POLL_SECONDS:-15}"
RCLONE_VERIFY_SIZE_RETRIES="${RCLONE_VERIFY_SIZE_RETRIES:-6}"
RCLONE_VERIFY_SIZE_DELAY="${RCLONE_VERIFY_SIZE_DELAY:-5}"
RCLONE_UPLOAD_MAX_DURATION="${RCLONE_UPLOAD_MAX_DURATION:-3h}"
RCLONE_PROGRESS_POLL_SECONDS="${RCLONE_PROGRESS_POLL_SECONDS:-10}"
RCLONE_PROGRESS_NOTICE_SECONDS="${RCLONE_PROGRESS_NOTICE_SECONDS:-60}"
LOCAL_TEMP="${LOCAL_TEMP:-/temp/fnos-media-import}"

PYTHON_BIN="${PYTHON_BIN:-python}"
APP_CALLBACK_URL="${APP_CALLBACK_URL:-}"
RCLONE_RUN_ID="${RCLONE_RUN_ID:-}"
RCLONE_TRIGGER_REASON="${RCLONE_TRIGGER_REASON:-}"
RCLONE_ONLY_CATEGORY="${RCLONE_ONLY_CATEGORY:-}"
RCLONE_ONLY_JOB_DIR="${RCLONE_ONLY_JOB_DIR:-}"
RCLONE_RETRY_EVENT_ID="${RCLONE_RETRY_EVENT_ID:-}"

RCLONE_COPY_MODE="${RCLONE_COPY_MODE:-false}"
RCLONE_COPY_SOURCE_REMOTE="${RCLONE_COPY_SOURCE_REMOTE:-}"
RCLONE_COPY_SOURCE_ROOT="${RCLONE_COPY_SOURCE_ROOT:-}"
RCLONE_COPY_TARGET_REMOTE="${RCLONE_COPY_TARGET_REMOTE:-}"
RCLONE_COPY_TARGET_ROOT="${RCLONE_COPY_TARGET_ROOT:-}"
RCLONE_COPY_LOCAL_TARGET="${RCLONE_COPY_LOCAL_TARGET:-}"
RCLONE_COPY_RELAY_MODE="${RCLONE_COPY_RELAY_MODE:-false}"
RCLONE_COPY_RELAY_ROOT="${RCLONE_COPY_RELAY_ROOT:-}"
RCLONE_COPY_RELAY_SEQUENTIAL="${RCLONE_COPY_RELAY_SEQUENTIAL:-false}"
RCLONE_COPY_RELAY_PLAN_KEY="${RCLONE_COPY_RELAY_PLAN_KEY:-}"
RCLONE_COPY_RELAY_FILE_INDEX="${RCLONE_COPY_RELAY_FILE_INDEX:-0}"
RCLONE_COPY_DELETE_SOURCE_AFTER_ACK="${RCLONE_COPY_DELETE_SOURCE_AFTER_ACK:-true}"

LOCK_FILE="${LOCK_FILE:-/tmp/fnos_media_import_rclone.lock}"
# 互斥锁被占用时的退出码：主程序按 75 识别“未启动、需稍后重排队”，
# 绝不能与 0（成功）或普通失败混用。
EXIT_LOCK_BUSY=75

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
rclone_cmd() {
  rclone "$@" --config "$RCLONE_CONFIG_PATH" --cache-dir "$RCLONE_CACHE_DIR"
}
container_exec() {
  "$@"
}
ensure_rclone_available() {
  if rclone version >/dev/null 2>&1; then
    return 0
  fi
  log "Worker 内的 rclone 命令不可用"
  exit 1
}
rclone_error_excerpt() {
  # 打印 rclone 真实错误的摘要（最多 3 行）：定位 remote 缺失、凭证失效、
  # 路径不存在等差异，避免只看到“创建失败”却不知道原因。
  err_file="$1"
  [ -f "$err_file" ] || return 0
  sed '/^[[:space:]]*$/d' "$err_file" 2>/dev/null | head -3 | sed 's/^/    rclone: /' >&2
}
ensure_remote_dir() {
  remote="$1"
  err_file="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-ensure.XXXXXX")" || return 1
  if rclone_cmd lsf "$remote" --max-depth 1 --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >/dev/null 2>"$err_file"; then
    rm -f "$err_file"
    return 0
  fi

  log "远端目录不存在或不可列出：${remote}"
  rclone_error_excerpt "$err_file"
  rm -f "$err_file"

  # 先区分 remote 级故障（配置缺失/凭证失效/不可达）与目录级缺失，
  # 两者的修复动作完全不同。
  remote_name="${remote%%:*}"
  remote_probe="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-probe.XXXXXX")" || return 1
  if rclone_cmd lsd "${remote_name}:" --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" >/dev/null 2>"$remote_probe"; then
    rm -f "$remote_probe"
    log "remote ${remote_name} 可访问，仅目录缺失，尝试创建：${remote}"
  else
    log "rclone remote ${remote_name} 不可访问：请检查 Worker 的 rclone.conf 是否包含该 remote（管理后台 rclone WebDAV 配置）、OpenList WebDAV 地址与凭证是否有效"
    rclone_error_excerpt "$remote_probe"
    rm -f "$remote_probe"
    return 1
  fi

  mkdir_err="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-mkdir.XXXXXX")" || return 1
  if ! rclone_cmd mkdir "$remote" >/dev/null 2>"$mkdir_err"; then
    log "远端目录创建失败：${remote}"
    rclone_error_excerpt "$mkdir_err"
    rm -f "$mkdir_err"
    return 1
  fi
  rm -f "$mkdir_err"

  if ! rclone_cmd lsf "$remote" --max-depth 1 --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "${RCLONE_LIST_MAX_DURATION:-10m}" >/dev/null 2>&1; then
    log "远端目录创建后仍不可访问：${remote}"
    return 1
  fi
  log "远端目录已就绪：${remote}"
  return 0
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
    if [ "$rc" -eq 74 ]; then
      log "${name} 因目标空间不足停止，不再重复下载；请释放中继盘空间后重试"
      return "$rc"
    fi
    if [ "$rc" -eq 75 ]; then
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
  if [ -z "$RCLONE_ONLY_JOB_DIR" ]; then
    rm -f "$snapshot_json"
    return 2
  fi
  if ! rclone_cmd lsjson "$remote" -R --files-only --include "/${RCLONE_ONLY_JOB_DIR}/**" --timeout "$RCLONE_TIMEOUT" --contimeout "$RCLONE_CONNECT_TIMEOUT" --max-duration "$RCLONE_LIST_MAX_DURATION" >"$snapshot_json"; then
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
  settle_seconds="${RCLONE_SOURCE_SETTLE_SECONDS:-10}"
  settle_rounds="${RCLONE_SOURCE_SETTLE_ROUNDS:-2}"
  appear_wait="${RCLONE_SOURCE_APPEAR_WAIT_SECONDS:-180}"
  appear_poll="${RCLONE_SOURCE_APPEAR_POLL_SECONDS:-15}"
  case "$settle_seconds" in ''|*[!0-9]*) settle_seconds=10 ;; esac
  case "$settle_rounds" in ''|*[!0-9]*) settle_rounds=2 ;; esac
  case "$appear_wait" in ''|*[!0-9]*) appear_wait=180 ;; esac
  case "$appear_poll" in ''|*[!0-9]*) appear_poll=15 ;; esac
  [ "$settle_seconds" -gt 0 ] || settle_seconds=10
  [ "$settle_rounds" -gt 0 ] || settle_rounds=1
  [ "$appear_poll" -gt 0 ] || appear_poll=15

  if ! first="$(list_remote_file_snapshots "$remote")"; then
    log "V1 source listing failed for ${job_name}; refusing an empty manifest"
    return 1
  fi
  waited=0
  while [ -z "$first" ] && [ "$waited" -lt "$appear_wait" ]; do
    stop_if_requested
    sleep_or_stop "$appear_poll" || return 143
    waited=$((waited + appear_poll))
    if ! first="$(list_remote_file_snapshots "$remote")"; then
      log "V1 source listing failed while waiting for ${job_name}"
      return 1
    fi
  done
  if [ -z "$first" ]; then
    log "V1 source directory is empty for ${job_name}"
    printf ''
    return 0
  fi

  round=1
  while [ "$round" -le "$settle_rounds" ]; do
    stop_if_requested
    sleep_or_stop "$settle_seconds" || return 143
    if ! second="$(list_remote_file_snapshots "$remote")"; then
      log "V1 source stability recheck failed for ${job_name}"
      return 1
    fi
    if [ "$first" = "$second" ]; then
      emit_snapshot_paths "$second"
      return $?
    fi
    first="$second"
    round=$((round + 1))
  done

  log "V1 source remained unstable; refusing a partial manifest for ${job_name}"
  return 1
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
normalize_job_dir() {
  printf '%s' "$1" | sed 's#\\#/#g; s#^[^/][^/]*:[/]*##; s#^/*##; s#/*$##' | tr '[:upper:]' '[:lower:]'
}
job_dirs_overlap() {
  left_remote="${3:-}"
  right_remote="${4:-}"
  # 只有两端处于同一 remote（或都未显式指定 remote，视为同一 remote）时才做
  # 路径重叠判断。不同 remote 之间即使路径字符串相同也合法（namespace 不同），
  # 不能仅凭路径误判重叠。
  if [ -n "$left_remote" ] && [ -n "$right_remote" ] && [ "$left_remote" != "$right_remote" ]; then
    return 1
  fi
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
http_callback_post() {
  CALLBACK_URL="$1" CALLBACK_RUN_ID="$2" CALLBACK_STATUS="$3" CALLBACK_CATEGORY="$4" CALLBACK_FILENAME="$5" CALLBACK_TARGET_PATH="$6" CALLBACK_SOURCE_PATH="$7" CALLBACK_MESSAGE="${8:-}" CALLBACK_MANIFEST_FILE="${9:-}" "$PYTHON_BIN" - <<'PY'
import json
import os
import posixpath
import re
import sys
import urllib.error
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
job_dir_match = re.fullmatch(r"job-([1-9][0-9]{0,8})", os.environ.get("RCLONE_ONLY_JOB_DIR", "").strip())
if not job_dir_match:
    print("V1 rclone callback requires a frozen job-N directory", file=sys.stderr)
    sys.exit(1)
payload["job_id"] = int(job_dir_match.group(1))
payload["require_job_match"] = True
custom_message = os.environ.get("CALLBACK_MESSAGE", "")
if custom_message:
    payload["message"] = custom_message
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
except urllib.error.HTTPError as exc:
    try:
        detail = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        detail = ""
    print(
        f"HTTP Error {exc.code}: {detail or exc.reason or 'server rejected callback'}",
        file=sys.stderr,
    )
    sys.exit(1)
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
callback_app() {
  callback_status="$1"
  callback_category="$2"
  callback_filename="$3"
  callback_target_path="$4"
  callback_source_path="$5"
  callback_message="${6:-}"
  callback_manifest_file="${7:-}"
  if [ -z "$APP_CALLBACK_URL" ]; then
    return 0
  fi

  callback_attempt=1
  while [ "$callback_attempt" -le 3 ]; do
    if http_callback_post "$APP_CALLBACK_URL" "$RCLONE_RUN_ID" "$callback_status" "$callback_category" "$callback_filename" "$callback_target_path" "$callback_source_path" "$callback_message" "$callback_manifest_file" >/dev/null; then
      return 0
    fi
    log "rclone callback failed: status=${callback_status}, category=${callback_category}, file=${callback_filename:-none}, attempt=${callback_attempt}/3"
    [ "$callback_attempt" -lt 3 ] && sleep "$callback_attempt"
    callback_attempt=$((callback_attempt + 1))
  done
  return 1
}
persist_staging_manifest() {
  manifest_content="$1"
  manifest_category="$2"
  manifest_target_root="$3"
  manifest_source_root="$4"
  if [ -z "$RCLONE_ONLY_JOB_DIR" ]; then
    log "V1 manifest requires a frozen job directory"
    return 1
  fi
  if [ -z "$APP_CALLBACK_URL" ]; then
    log "任务级暂存缺少回调地址，拒绝在未持久化 manifest 时搬运"
    return 1
  fi
  manifest_file="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-manifest.XXXXXX")" || return 1
  printf '%s\n' "$manifest_content" >"$manifest_file"
  manifest_count="$(line_count "$manifest_content")"
  if callback_app "staging_manifest" "$manifest_category" "" "$manifest_target_root" "$manifest_source_root" "rclone 已固化任务级源文件清单：${manifest_count} 个文件" "$manifest_file"; then
    rm -f "$manifest_file"
    return 0
  fi
  rm -f "$manifest_file"
  return 1
}
normalize_remote_path() {
  # 远端相对路径：归一化斜杠、去掉首尾斜杠（rclone remote 路径不允许前导 /）。
  printf '%s' "$1" | sed 's#\\#/#g; s#//*#/#g; s#^/*##; s#/*$##'
}
normalize_local_path() {
  # 容器本地路径：保留前导 /（绝对路径），只归一化斜杠和去掉尾部斜杠。
  printf '%s' "$1" | sed 's#\\#/#g; s#//*#/#g; s#/*$##'
}
copy_local_size() {
  copy_local_size_file="$1"
  container_exec stat -c '%s' "$copy_local_size_file" 2>/dev/null || true
}
wait_local_size_match() {
  wait_local_file="$1"
  wait_local_expected_size="$2"
  wait_local_label="${3:-$wait_local_file}"
  wait_local_retries="${RCLONE_VERIFY_SIZE_RETRIES:-6}"
  wait_local_delay="${RCLONE_VERIFY_SIZE_DELAY:-5}"
  case "$wait_local_retries" in
    ''|*[!0-9]*) wait_local_retries=1 ;;
  esac
  case "$wait_local_delay" in
    ''|*[!0-9]*) wait_local_delay=1 ;;
  esac
  [ "$wait_local_retries" -gt 0 ] || wait_local_retries=1

  attempt=1
  while [ "$attempt" -le "$wait_local_retries" ]; do
    stop_if_requested
    actual_size="$(copy_local_size "$wait_local_file")"
    if [ -n "$wait_local_expected_size" ] && [ "$actual_size" = "$wait_local_expected_size" ]; then
      [ "$attempt" -gt 1 ] && log "本地大小确认通过：${wait_local_label} size=${actual_size}"
      return 0
    fi
    log "本地大小确认未通过：${wait_local_label} source=${wait_local_expected_size:-unknown} target=${actual_size:-none}，第 ${attempt}/${wait_local_retries} 次"
    attempt=$((attempt + 1))
    [ "$attempt" -le "$wait_local_retries" ] && sleep_or_stop "$wait_local_delay" || true
    stop_if_requested
  done
  return 1
}
copy_wait_size_match() {
  copy_target_spec="$1"
  copy_expected_size="$2"
  copy_label="$3"
  copy_is_local_target="$4"
  if [ "$copy_is_local_target" = "1" ]; then
    wait_local_size_match "$copy_target_spec" "$copy_expected_size" "$copy_label"
  else
    wait_remote_size_match "$copy_target_spec" "$copy_expected_size" "$copy_label"
  fi
}
# rclone 原生统计只输出英文且不支持本地化；--stats-one-line 让每次统计只占一行，
# 这里把该行翻译成中文进度（方向词由调用处经 RCLONE_PROGRESS_DIRECTION 注入：
# 下载/上传，缺省退回“传输”），其余输出（错误、NOTICE 摘要等）原样透传，
# 保证排障信息不受翻译影响。
# fflush 只刷新输出，不能解除部分 awk 实现对管道输入的整块读取等待。
# 长时间运行的 rclone 必须先经过 stream_progress_filter 的逐行输入边界。
progress_filter() {
  # progress_direction 由调用处注入（下载/上传），缺省时退回“传输”。
  # progress_marker 记录已观察到的进度统计值；0% 也只是统计，不代表已收到字节。
  awk -v progress_label="${RCLONE_PROGRESS_LABEL:-}" -v progress_direction="${RCLONE_PROGRESS_DIRECTION:-}" -v progress_marker="${RCLONE_PROGRESS_MARKER:-}" '
    /NOTICE/ && /ETA/ {
      line = $0
      sub(/^.*NOTICE:[ \t]*/, "", line)
      # 单文件 copyto 的统计行自带 "<文件名>: " 前缀，剥掉避免与中文标签重复。
      if (line ~ /^[^,]*:[ \t]/) {
        sub(/^[^,]*:[ \t]*/, "", line)
      }
      # 统计行逗号字段数随 rclone 版本不同（3 段 "<进度>,<速度>,ETA <剩余>" 或
      # 4 段 "<进度>,<已传/总量>,<速度>,ETA <剩余>"），按“末段=ETA、倒数第
      # 二段=速度、其余并入进度”归一化，避免写死字段数漏匹配。
      n = split(line, parts, ",[ \t]*")
      if (n >= 3 && parts[n] ~ /^ETA[ \t]/) {
        gsub(/^ETA[ \t]+/, "", parts[n])
        if (parts[n] == "-") {
          parts[n] = "计算中"
        }
        speed = parts[n-1]
        progress = parts[1]
        for (i = 2; i < n - 1; i++) {
          progress = progress "，" parts[i]
        }
        if (progress_marker != "") {
          print "" > progress_marker
          print progress >> progress_marker
          close(progress_marker)
        }
        direction = (progress_direction == "" ? "传输" : progress_direction)
        printf "%s中%s：%s，速度 %s，预计剩余 %s\n", \
          direction, (progress_label == "" ? "" : " " progress_label), progress, speed, parts[n]
        fflush()
        next
      }
    }
    { print; fflush() }
  '
}
stream_progress_filter() {
  # POSIX read 在换行时立即返回；每行给 awk 独立 EOF，避免 mawk 等实现把
  # 稀疏统计积压在输入缓冲中。保留现有格式化器，不依赖 stdbuf 或特定 awk 参数。
  # rclone 每 5 秒一条统计，这里的短进程开销远小于一次网络请求。
  while IFS= read -r copy_progress_line || [ -n "$copy_progress_line" ]; do
    case "$copy_progress_line" in
      *"no space left on device"*|*"disk quota exceeded"*)
        if [ -n "${RCLONE_STORAGE_FULL_MARKER:-}" ]; then
          printf '1\n' >"$RCLONE_STORAGE_FULL_MARKER" || true
        fi
        ;;
    esac
    printf '%s\n' "$copy_progress_line" | progress_filter
  done
}
copy_file() {
  copy_source_spec="$1"
  copy_dest_spec="$2"
  # POSIX sh 没有 PIPESTATUS / 进程替换：用 rc 文件从管道里带回 rclone 退出码。
  copy_rc_file="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-rc.XXXXXX")"
  RCLONE_STORAGE_FULL_MARKER="${copy_rc_file}.storage-full"
  export RCLONE_STORAGE_FULL_MARKER
  # 看门狗覆盖整个 copy 调用：无统计或统计值长期不变时定期反馈，不能把
  # 0% 当成首字节到达，也不能在首条统计后永久停止观察。
  copy_progress_marker="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-progress.XXXXXX")"
  rm -f "$copy_progress_marker"
  RCLONE_PROGRESS_LABEL="$(basename "$copy_dest_spec")"
  RCLONE_PROGRESS_MARKER="$copy_progress_marker"
  export RCLONE_PROGRESS_LABEL RCLONE_PROGRESS_DIRECTION RCLONE_PROGRESS_MARKER
  (
    copy_poll_seconds="$RCLONE_PROGRESS_POLL_SECONDS"
    copy_notice_seconds="$RCLONE_PROGRESS_NOTICE_SECONDS"
    case "$copy_poll_seconds" in ''|*[!0-9]*|0*) copy_poll_seconds=10 ;; esac
    case "$copy_notice_seconds" in ''|*[!0-9]*|0*) copy_notice_seconds=60 ;; esac
    copy_idle_seconds=0
    copy_next_notice="$copy_notice_seconds"
    copy_last_progress=""
    copy_seen_progress=0
    copy_sleep_pid=""
    # 不留下仍持有日志管道或 flock fd 的 sleep 子进程。
    trap 'if [ -n "$copy_sleep_pid" ]; then kill "$copy_sleep_pid" 2>/dev/null || true; wait "$copy_sleep_pid" 2>/dev/null || true; fi; exit 0' INT TERM
    while :; do
      sleep "$copy_poll_seconds" &
      copy_sleep_pid=$!
      wait "$copy_sleep_pid" || exit 0
      copy_sleep_pid=""
      if [ -e "$copy_progress_marker" ]; then
        copy_seen_progress=1
        copy_current_progress="$(cat "$copy_progress_marker" 2>/dev/null || true)"
        if [ "$copy_current_progress" != "$copy_last_progress" ]; then
          copy_last_progress="$copy_current_progress"
          copy_idle_seconds=0
          copy_next_notice="$copy_notice_seconds"
          continue
        fi
      fi
      copy_idle_seconds=$((copy_idle_seconds + copy_poll_seconds))
      if [ "$copy_idle_seconds" -ge "$copy_next_notice" ]; then
        if [ "$copy_seen_progress" -eq 0 ]; then
          log "等待进度：${RCLONE_PROGRESS_LABEL} 约 ${copy_idle_seconds} 秒未收到 rclone 进度统计，搬运调用尚未结束；请检查源端响应或网络状态"
        else
          log "进度暂未变化：${RCLONE_PROGRESS_LABEL} 约 ${copy_idle_seconds} 秒未观察到进度统计值更新，搬运调用尚未结束，不能据此判定成功或失败"
        fi
        copy_next_notice=$((copy_next_notice + copy_notice_seconds))
      fi
    done
  ) &
  copy_watchdog_pid=$!
  {
    copy_file_once "$copy_source_spec" "$copy_dest_spec"
    echo "$?" >"$copy_rc_file"
  } 2>&1 | stream_progress_filter >&2
  kill "$copy_watchdog_pid" 2>/dev/null || true
  wait "$copy_watchdog_pid" 2>/dev/null || true
  copy_rc="$(cat "$copy_rc_file" 2>/dev/null || echo 1)"
  if [ "$copy_rc" -ne 0 ] && [ -f "$RCLONE_STORAGE_FULL_MARKER" ]; then
    copy_rc=74
  fi
  rm -f "$copy_rc_file" "$copy_progress_marker" "$RCLONE_STORAGE_FULL_MARKER"
  return "$copy_rc"
}
relay_copy_cmd() {
  if [ "${copy_relay_version:-1}" != "2" ]; then
    rclone_cmd "$@"
    return "$?"
  fi
  if relay_file_state check-space; then :; else return "$?"; fi
  # Use the exact child PID, never a process-wide signal: an unrelated Job's
  # upload and all acknowledged cache files must remain untouched.
  relay_guard_marker="${copy_rc_file}.relay-deferred"
  # A bounded slot owns one deterministic local path. Random .partial names
  # would leave unaccounted bytes and block safe resumption after a space stop.
  rclone "$@" --inplace --config "$RCLONE_CONFIG_PATH" --cache-dir "$RCLONE_CACHE_DIR" &
  relay_copy_pid=$!
  (
    relay_guard_sleep=""
    trap 'if [ -n "$relay_guard_sleep" ]; then kill "$relay_guard_sleep" 2>/dev/null || true; wait "$relay_guard_sleep" 2>/dev/null || true; fi; exit 0' INT TERM
    while kill -0 "$relay_copy_pid" 2>/dev/null; do
      if relay_file_state check-space; then
        :
      else
        relay_guard_rc=$?
        printf '%s\n' "$relay_guard_rc" >"$relay_guard_marker"
        kill -TERM "$relay_copy_pid" 2>/dev/null || true
        exit 0
      fi
      sleep 1 &
      relay_guard_sleep=$!
      wait "$relay_guard_sleep" || exit 0
      relay_guard_sleep=""
    done
  ) &
  relay_guard_pid=$!
  if wait "$relay_copy_pid"; then relay_copy_rc=0; else relay_copy_rc=$?; fi
  kill "$relay_guard_pid" 2>/dev/null || true
  wait "$relay_guard_pid" 2>/dev/null || true
  if [ -f "$relay_guard_marker" ]; then
    relay_copy_rc="$(cat "$relay_guard_marker")"
    rm -f "$relay_guard_marker"
  fi
  return "$relay_copy_rc"
}
copy_file_once() {
  copy_source_spec="$1"
  copy_dest_spec="$2"
  if [ -n "$RCLONE_UPLOAD_MAX_DURATION" ] && [ "$RCLONE_UPLOAD_MAX_DURATION" != "0" ]; then
    relay_copy_cmd copyto "$copy_source_spec" "$copy_dest_spec" \
      --transfers 1 \
      --checkers 1 \
      --no-traverse \
      --disable Copy \
      --buffer-size "$BUFFER_SIZE" \
      --retries "$RCLONE_UPLOAD_RETRIES" \
      --low-level-retries "$RCLONE_LOW_LEVEL_RETRIES" \
      --retries-sleep "${RETRY_DELAY}s" \
      --timeout "$RCLONE_TIMEOUT" \
      --contimeout "$RCLONE_CONNECT_TIMEOUT" \
      --max-duration "$RCLONE_UPLOAD_MAX_DURATION" \
      --stats 5s \
      --stats-one-line \
      --stats-log-level NOTICE
  else
    relay_copy_cmd copyto "$copy_source_spec" "$copy_dest_spec" \
      --transfers 1 \
      --checkers 1 \
      --no-traverse \
      --disable Copy \
      --buffer-size "$BUFFER_SIZE" \
      --retries "$RCLONE_UPLOAD_RETRIES" \
      --low-level-retries "$RCLONE_LOW_LEVEL_RETRIES" \
      --retries-sleep "${RETRY_DELAY}s" \
      --timeout "$RCLONE_TIMEOUT" \
      --contimeout "$RCLONE_CONNECT_TIMEOUT" \
      --stats 5s \
      --stats-one-line \
      --stats-log-level NOTICE
  fi
}
relay_file_state() {
  if [ "$#" -gt 1 ]; then
    "$PYTHON_BIN" -m fnos_media_import.transfer.relay_file_state "$1" \
      --local-dir "$copy_target_dir" --job-id "${copy_job_dir#job-}" \
      --plan-key "$RCLONE_COPY_RELAY_PLAN_KEY" --index "$RCLONE_COPY_RELAY_FILE_INDEX" --size "$2"
  else
    "$PYTHON_BIN" -m fnos_media_import.transfer.relay_file_state "$1" \
      --local-dir "$copy_target_dir" --job-id "${copy_job_dir#job-}" \
      --plan-key "$RCLONE_COPY_RELAY_PLAN_KEY" --index "$RCLONE_COPY_RELAY_FILE_INDEX"
  fi
}
prepare_relay_file() {
  relay_waited=0
  relay_wait_limit="${TRANSFER_WAIT_TIMEOUT_SECONDS:-10800}"
  case "$relay_wait_limit" in ''|*[!0-9]*|0*) relay_wait_limit=10800 ;; esac
  while :; do
    stop_if_requested
    if relay_file_state prepare "$1"; then
      return 0
    else
      relay_prepare_rc=$?
    fi
    if [ "$relay_prepare_rc" -eq 78 ]; then
      log "RELAY_CACHE_IMPOSSIBLE: 单个文件超过中继缓存上限，请提高 RELAY_CACHE_MAX_BYTES 或跳过该文件"
      return 78
    fi
    if [ "$relay_prepare_rc" -ne 75 ]; then return "$relay_prepare_rc"; fi
    if [ "${copy_relay_version:-1}" = "2" ]; then
      log "RELAY_CACHE_DEFERRED: 中继缓存预算暂不可用，释放下载锁并交回队列等待"
      return 75
    fi
    if [ "$relay_waited" -ge "$relay_wait_limit" ]; then
      log "等待其它任务释放中继缓存超时，本轮未下载，保留来源供人工核对"
      return 1
    fi
    log "等待其它中继文件上传、验证并释放缓存；不会把正常排队当作下载失败重试"
    sleep_or_stop 10 || return 143
    relay_waited=$((relay_waited + 10))
  done
}
copy_scoped_job() {
  stop_if_requested
  ensure_rclone_available

  if [ -z "$RCLONE_ONLY_JOB_DIR" ]; then
    log "rclone_copy 模式必须绑定具体任务目录（RCLONE_ONLY_JOB_DIR），拒绝按后台配置扫描全部任务"
    return 2
  fi
  if [ -z "$APP_CALLBACK_URL" ]; then
    log "rclone_copy 模式缺少主程序回调地址（APP_CALLBACK_URL），无法获得完成确认，拒绝执行"
    return 2
  fi

  copy_source_remote="$RCLONE_COPY_SOURCE_REMOTE"
  copy_source_root="$(normalize_remote_path "$RCLONE_COPY_SOURCE_ROOT")"
  copy_target_remote="$RCLONE_COPY_TARGET_REMOTE"
  copy_target_root="$(normalize_remote_path "$RCLONE_COPY_TARGET_ROOT")"
  copy_job_dir="$(normalize_remote_path "$RCLONE_ONLY_JOB_DIR")"
  if ! printf '%s' "$copy_job_dir" | grep -Eq '^job-[1-9][0-9]{0,8}$'; then
    log "rclone_copy 模式任务目录非法：${RCLONE_ONLY_JOB_DIR}"
    return 2
  fi
  if [ -z "$copy_source_remote" ]; then
    log "rclone_copy 缺少冻结的源 remote"
    return 2
  fi

  copy_is_local=0
  copy_local_root=""
  if [ -n "$RCLONE_COPY_LOCAL_TARGET" ]; then
    copy_is_local=1
    copy_local_root="$(normalize_local_path "$RCLONE_COPY_LOCAL_TARGET")"
    if [ -z "$copy_local_root" ] || [ "$copy_local_root" = "." ]; then
      log "rclone_copy 本地目标根目录非法：${RCLONE_COPY_LOCAL_TARGET}"
      return 2
    fi
    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ]; then
      copy_relay_root="$(normalize_local_path "$RCLONE_COPY_RELAY_ROOT")"
      if [ -z "$copy_relay_root" ] || [ "$copy_relay_root" = "." ]; then
        log "rclone_copy relay mode is missing a controlled relay root"
        return 2
      fi
      case "$copy_local_root" in
        "$copy_relay_root"|"$copy_relay_root"/*) ;;
        *)
          log "rclone_copy relay target is outside the controlled relay root: ${RCLONE_COPY_LOCAL_TARGET}"
          return 2
          ;;
      esac
    else
      case "$copy_local_root" in
        /storage-targets|/storage-targets/*) ;;
        *)
          log "rclone_copy 本地目标不在 /storage-targets 受控挂载内：${RCLONE_COPY_LOCAL_TARGET}"
          return 2
          ;;
      esac
    fi
    copy_local_temp_norm="$(normalize_local_path "$LOCAL_TEMP")"
    if [ -n "$copy_local_temp_norm" ]; then
      case "$copy_local_root" in
        "$copy_local_temp_norm"|"$copy_local_temp_norm"/*)
          log "rclone_copy 本地目标落在本地 temp 缓存目录内，拒绝执行：${RCLONE_COPY_LOCAL_TARGET}"
          return 2
          ;;
      esac
    fi
  fi

  copy_source_dir="${copy_source_root:+${copy_source_root}/}${copy_job_dir}"
  source_remote_dir="${copy_source_remote}:${copy_source_dir}"
  if [ "$copy_is_local" -eq 1 ]; then
    copy_target_dir="${copy_local_root}/${copy_job_dir}"
  else
    if [ -z "$copy_target_remote" ]; then
      log "rclone_copy 缺少冻结的目标 remote"
      return 2
    fi
    copy_target_dir="${copy_target_root:+${copy_target_root}/}${copy_job_dir}"
    target_remote_dir="${copy_target_remote}:${copy_target_dir}"
  fi

  # 源目标重叠防护（remote 身份 + 归一化路径）：同一 remote 且路径互相包含时
  # 拒绝执行，避免循环复制把文件写回源端。
  if [ "$copy_is_local" -ne 1 ]; then
    if job_dirs_overlap "$copy_source_dir" "$copy_target_dir" "$copy_source_remote" "$copy_target_remote"; then
      log "rclone_copy 源与目标处于同一 remote 且路径重叠，拒绝执行以避免循环复制：${source_remote_dir} -> ${target_remote_dir}"
      return 1
    fi
  fi

  # 本地中继目标 = 从源网盘下载；远端目标 = 流式上传到目标网盘。
  # 进度行与文件级日志共用该方向词，避免“传输中”分不清上传还是下载。
  if [ "$copy_is_local" -eq 1 ]; then
    copy_direction_label="下载"
  else
    copy_direction_label="上传"
  fi
  RCLONE_PROGRESS_DIRECTION="$copy_direction_label"
  export RCLONE_PROGRESS_DIRECTION

  if [ "$copy_is_local" -eq 1 ]; then
    log "rclone_copy 模式启动：${source_remote_dir} -> ${copy_target_dir}"
  else
    log "rclone_copy 模式启动：${source_remote_dir} -> ${target_remote_dir}"
  fi

  if ! ensure_remote_dir "$source_remote_dir"; then
    log "rclone_copy 源任务目录不可用，本轮拒绝执行：${source_remote_dir}"
    log "排查提示：管理后台 OpenList 挂载的 webdav_root 必须与 OpenList 中该存储的实际挂载路径一致，且来源账号的原生目录应指向网盘内同一目录；夸克转存完成但 rclone 列不到，通常就是这两处路径没对上"
    return 1
  fi
  if [ "$copy_is_local" -eq 1 ]; then
    if ! container_exec mkdir -p "$copy_target_dir"; then
      log "rclone_copy 本地目标目录创建失败：${copy_target_dir}"
      return 1
    fi
    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ]; then
      copy_control_root="$copy_relay_root"
    else
      copy_control_root="/storage-targets"
    fi
    copy_control_real="$(container_exec readlink -f "$copy_control_root" 2>/dev/null || true)"
    copy_root_real="$(container_exec readlink -f "$copy_local_root" 2>/dev/null || true)"
    copy_target_real="$(container_exec readlink -f "$copy_target_dir" 2>/dev/null || true)"
    if [ -z "$copy_control_real" ] || [ -z "$copy_root_real" ] || [ -z "$copy_target_real" ]; then
      log "rclone_copy cannot resolve the controlled local target"
      return 1
    fi
    case "$copy_root_real" in
      "$copy_control_real"|"$copy_control_real"/*) ;;
      *)
        log "rclone_copy local target escaped its controlled root: ${copy_local_root} -> ${copy_root_real}"
        return 1
        ;;
    esac
    case "$copy_target_real" in
      "$copy_root_real"|"$copy_root_real"/*) ;;
      *)
        log "rclone_copy 任务目录通过符号链接越出目标根，拒绝执行：${copy_target_dir} -> ${copy_target_real}"
        return 1
        ;;
    esac
  elif ! ensure_remote_dir "$target_remote_dir"; then
    log "rclone_copy 目标任务目录不可用，本轮拒绝执行：${target_remote_dir}"
    return 1
  fi

  # 复用任务级暂存稳定性清单语义：以源根 remote 为列目录起点，include 只观察
  # 当前 job 目录，强制稳定性等待、拒绝固化半批清单。返回的路径带 job-<id>/ 前缀。
  copy_source_list_remote="${copy_source_remote}:${copy_source_root}"
  [ -n "$copy_source_root" ] || copy_source_list_remote="${copy_source_remote}:"
  if copy_file_list="$(stable_file_list "$copy_source_list_remote" "$copy_job_dir")"; then
    copy_list_rc=0
  else
    copy_list_rc=$?
  fi
  if [ "$copy_list_rc" -ne 0 ]; then
    if is_interrupted_rc "$copy_list_rc"; then
      log "rclone_copy 源文件清单读取被停止请求中断，本轮不处理不删除任何文件"
      return "$copy_list_rc"
    fi
    log "rclone_copy 源文件清单读取失败，本轮不处理不删除任何文件：${source_remote_dir}"
    return "$copy_list_rc"
  fi
  if [ -z "$copy_file_list" ]; then
    log "rclone_copy 源任务目录没有待复制文件：${copy_job_dir}"
    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ]; then
      log "中继下载没有可上传文件，本轮失败并保留来源等待重试"
      return 1
    fi
    return 0
  fi
  log "rclone_copy 源文件数：$(line_count "$copy_file_list")"
  if ! persist_staging_manifest "$copy_file_list" "$RCLONE_ONLY_CATEGORY" "$copy_target_root" "$copy_source_root"; then
    log "rclone_copy 文件清单未被应用确认，本轮不复制也不删除任何源文件"
    return 1
  fi

  if [ "$RCLONE_COPY_RELAY_MODE" = "true" ] && [ "$RCLONE_COPY_RELAY_SEQUENTIAL" = "true" ]; then
    if ! copy_file_list="$(printf '%s\n' "$copy_file_list" | relay_file_state select)"; then
      log "逐文件中继清单核验失败，本轮不下载也不删除文件"
      return 1
    fi
    if [ -z "$copy_file_list" ]; then
      log "当前文件已有持久回执，不重复下载，交由中继协调器核验上传和清理"
      return 0
    fi
    # Freeze the new budget before even the first file is downloaded. A v1
    # store remains readable, but new workers must not bypass the v2 quota.
    if relay_file_state enable; then
      :
    else
      relay_enable_rc=$?
      if [ "$relay_enable_rc" -eq 75 ]; then
        log "RELAY_CACHE_DEFERRED: 等待其它任务释放中继缓存，尚未下载首文件，交回调度队列"
        return 75
      fi
      log "有界中继预算初始化失败，保留现有文件并停止下载"
      return "$relay_enable_rc"
    fi
    if ! copy_relay_version="$(relay_file_state version)"; then
      log "逐文件中继版本核验失败，保留现有文件并停止下载"
      return 1
    fi
    if [ "$copy_relay_version" = "2" ]; then
      log "有界中继预下载：本轮仅下载 ${copy_file_list}，上传、验证和清理仍逐文件有序执行"
    else
      log "逐文件中继：本轮仅下载 ${copy_file_list}，上传、远端验证和缓存释放后才处理下一文件"
    fi
  fi

  copied_count=0
  failed_count=0
  while IFS= read -r file; do
    stop_if_requested
    [ -z "$file" ] && continue
    case "$file" in
      \[20[0-9][0-9]-*)
        log "跳过疑似日志污染行，避免误当文件复制：${file}"
        continue
        ;;
    esac

    # 清单路径带 job-<id>/ 前缀（复用任务级暂存清单语义），复制时精确剥离
    # 该任务目录前缀，得到 job 目录内的相对路径。
    copy_rel_path="$file"
    case "$copy_rel_path" in
      "$copy_job_dir"/*) copy_rel_path="${copy_rel_path#${copy_job_dir}/}" ;;
    esac
    copy_basename="$(basename "$file")"
    source_file_remote="${source_remote_dir}/${copy_rel_path}"
    if [ "$copy_is_local" -eq 1 ]; then
      target_file="${copy_target_dir}/${copy_rel_path}"
    else
      target_file="${target_remote_dir}/${copy_rel_path}"
    fi
    source_callback_path="${copy_source_root:+${copy_source_root}/}${file}"
    if [ "$copy_is_local" -eq 1 ]; then
      target_callback_path="${RCLONE_COPY_LOCAL_TARGET}/${file}"
    else
      target_callback_path="${copy_target_root:+${copy_target_root}/}${file}"
    fi

    source_size="$(remote_file_size "$source_file_remote")"
    if [ -z "$source_size" ]; then
      log "rclone_copy 源文件大小读取失败，保留源端：${source_file_remote}"
      failed_count=$((failed_count + 1))
      callback_app "failed" "$RCLONE_ONLY_CATEGORY" "$copy_basename" "$target_callback_path" "$source_callback_path" "源文件大小读取失败，保留源端" || true
      continue
    fi

    log "${copy_direction_label}文件：${file} size=${source_size}"
    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ] && [ "$RCLONE_COPY_RELAY_SEQUENTIAL" = "true" ]; then
      if prepare_relay_file "$source_size"; then
        :
      else
        relay_prepare_rc=$?
        if is_interrupted_rc "$relay_prepare_rc"; then return "$relay_prepare_rc"; fi
        if [ "$relay_prepare_rc" -eq 78 ]; then return 78; fi
        if [ "$relay_prepare_rc" -eq 75 ]; then return 75; fi
        log "当前文件空间或状态检查失败，停止本轮搬运；保留来源，不重试下载也不处理下一文件"
        return 1
      fi
    fi
    if [ "$copy_is_local" -eq 1 ]; then
      copy_local_parent="$(dirname "$target_file")"
      container_exec mkdir -p "$copy_local_parent" >/dev/null 2>&1 || true
    fi

    copy_reuse_local=0
    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ] && [ "$RCLONE_COPY_RELAY_SEQUENTIAL" = "true" ] \
      && [ "$(copy_local_size "$target_file")" = "$source_size" ]; then
      copy_reuse_local=1
      log "复用大小已核验的任务级本地缓存，不重复下载：${file}"
    fi
    if [ "$copy_reuse_local" -eq 1 ] || retry_count "${copy_direction_label} ${copy_basename}" "$MAX_RETRIES" copy_file "$source_file_remote" "$target_file"; then
      :
    else
      copy_result=$?
      if [ "${copy_relay_version:-1}" = "2" ] && { [ "$copy_result" -eq 75 ] || [ "$copy_result" -eq 74 ]; }; then
        log "RELAY_CACHE_DEFERRED: 中继盘空间安全余量不足，停止当前下载并保留来源及缓存，等待重新调度"
        return 75
      fi
      if is_interrupted_rc "$copy_result"; then
        log "复制被停止请求中断，保留源端：${file}"
        callback_app "stopped" "$RCLONE_ONLY_CATEGORY" "$copy_basename" "$target_callback_path" "$source_callback_path" "复制被停止请求中断，保留源端" || true
        return "$copy_result"
      fi
      log "复制失败，保留源端等待下次重试：${file}"
      callback_app "failed" "$RCLONE_ONLY_CATEGORY" "$copy_basename" "$target_callback_path" "$source_callback_path" "复制失败，保留源端" || true
      failed_count=$((failed_count + 1))
      continue
    fi

    # 目标端大小校验（remote 目标复用 wait_remote_size_match，本地目标走 stat）。
    if ! copy_wait_size_match "$target_file" "$source_size" "$file" "$copy_is_local"; then
      log "目标端大小校验未通过，保留源端：${file}"
      callback_app "failed" "$RCLONE_ONLY_CATEGORY" "$copy_basename" "$target_callback_path" "$source_callback_path" "目标端大小校验未通过，保留源端" || true
      failed_count=$((failed_count + 1))
      continue
    fi

    # 应用完成回调 ACK：未确认则不删除源端。
    if ! callback_app "done" "$RCLONE_ONLY_CATEGORY" "$copy_basename" "$target_callback_path" "$source_callback_path"; then
      log "完成回调未确认，保留源端等待下次重试：${file}"
      failed_count=$((failed_count + 1))
      continue
    fi

    if [ "$RCLONE_COPY_RELAY_MODE" = "true" ] && [ "$RCLONE_COPY_RELAY_SEQUENTIAL" = "true" ]; then
      if ! relay_file_state downloaded "$source_size"; then
        log "单文件下载回执未持久化，保留缓存和来源，拒绝移交上传"
        return 1
      fi
      log "单文件下载已确认：${file}；等待原生上传、远端验证和本地缓存释放，来源保留至整任务完成"
      return 0
    fi

    stop_if_requested
    if [ "$RCLONE_COPY_DELETE_SOURCE_AFTER_ACK" != "true" ]; then
      log "执行计划要求保留源文件，已完成目标确认：${source_file_remote}"
      copied_count=$((copied_count + 1))
      continue
    fi

    if ! rclone_cmd deletefile "$source_file_remote" >/dev/null 2>&1; then
      log "源端文件删除失败，已保留源端：${source_file_remote}"
      failed_count=$((failed_count + 1))
      continue
    fi
    log "源端文件已删除：${source_file_remote}"
    copied_count=$((copied_count + 1))

    # 剪除源端空目录（保留 job 根目录，末尾 rmdirs --leave-root 统一清理）。
    copy_rel_dir="$(dirname "$copy_rel_path")"
    if [ "$copy_rel_dir" != "." ]; then
      rclone_cmd rmdir "${source_remote_dir}/${copy_rel_dir}" >/dev/null 2>&1 || true
    fi
  done <<EOF
$copy_file_list
EOF

  stop_if_requested
  rclone_cmd rmdirs "$source_remote_dir" --leave-root >/dev/null 2>&1 || true

  if [ "$copy_is_local" -eq 1 ] && [ "$RCLONE_COPY_RELAY_MODE" = "true" ]; then
    if [ "$copied_count" -eq 0 ]; then
      log "中继下载未确认任何文件，拒绝标记成功或移交上传"
      return 1
    fi
    if [ "$failed_count" -ne 0 ]; then
      log "中继下载尚有 ${failed_count} 个失败文件，本轮未移交上传，保留已下载文件等待恢复"
      return 1
    fi
    # 下载只是中继链路第一段：结束后还有网盘 API 上传与整理，
    # 结束语必须如实说明“移交”，不能让用户误以为整个任务已完成。
    log "rclone 下载完成：${copy_job_dir}，下载 ${copied_count} 个文件，失败 ${failed_count} 个，已移交网盘上传"
  else
    log "rclone_copy 任务结束：${copy_job_dir}，${copy_direction_label} ${copied_count} 个文件，失败 ${failed_count} 个文件"
  fi
  [ "$failed_count" -eq 0 ]
}
acquire_lock() {
  # 用 flock 持有锁文件描述符：进程退出（含 SIGKILL、容器硬重启）时内核自动
  # 释放，不再像 mkdir 锁那样残留目录、把后续所有任务都挡在门外。
  # 拿不到锁时用独立退出码 75 退出，主程序据此把本次 run 回滚到队列，
  # 而不是当成搬运成功去唤醒上传。
  if ! command -v flock >/dev/null 2>&1; then
    log "Worker 内缺少 flock 命令，无法建立 rclone 互斥锁"
    exit 1
  fi
  exec 9>"$LOCK_FILE"
  if flock -n 9; then
    return 0
  fi

  log "已有 rclone 搬运任务运行中，本次未启动，稍后由队列重新调度"
  exit "$EXIT_LOCK_BUSY"
}
main() {
  acquire_lock

  log "飞牛影视 rclone 搬运脚本启动 ${SCRIPT_VERSION}"
  if [ "$RCLONE_COPY_MODE" != "true" ]; then
    log "V1 硬切换只允许冻结任务触发的 rclone_copy；拒绝全局目录扫描"
    exit 2
  fi
  log "rclone_copy 直连复制模式，仅执行冻结任务描述符"
  set +e
  copy_scoped_job
  copy_rc=$?
  set -e
  if is_interrupted_rc "$copy_rc"; then
    log "rclone_copy 已被停止请求中断，保留源端，exit=${copy_rc}"
    exit "$copy_rc"
  fi
  log "rclone_copy 执行结束，exit=${copy_rc}"
  exit "$copy_rc"
}
trap 'request_stop' INT TERM

main "$@"
