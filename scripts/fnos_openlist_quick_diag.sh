#!/bin/sh
# FNOS OpenList quick observer. Read-only except for its own log files.

set -u
umask 077

OPENLIST_DATA=${OPENLIST_DATA:-/vol1/@appdata/OpenList/data}
OPENLIST_LOG=${OPENLIST_LOG:-$OPENLIST_DATA/log/log.log}
DIAG_DIR=${DIAG_DIR:-$OPENLIST_DATA/diagnostics}
OPENLIST_USER=${OPENLIST_USER:-OpenList}
OPENLIST_NAME=${OPENLIST_NAME:-openlist}
OPENLIST_PORT=${OPENLIST_PORT:-5255}
POLL_SECONDS=${POLL_SECONDS:-5}
PROBE_SECONDS=${PROBE_SECONDS:-60}
SETTLE_SECONDS=${SETTLE_SECONDS:-5}
CONNECT_SECONDS=${CONNECT_SECONDS:-5}
REQUEST_SECONDS=${REQUEST_SECONDS:-12}
MAX_IPS=${MAX_IPS:-4}

HOSTS='user-njs.yun.139.com personal-kd-njs.yun.139.com aas.caiyun.feixin.10086.cn mail.10086.cn smsrebuild1.mail.10086.cn'

mkdir -p "$DIAG_DIR" || exit 1
chmod 700 "$DIAG_DIR" 2>/dev/null || true
SESSION="$(date '+%Y%m%d-%H%M%S')-$$"
LOG="$DIAG_DIR/quick-$SESSION.log"
PID_FILE="/tmp/fnos-openlist-quick-diag.pid"
touch "$LOG" || exit 1
printf '%s\n' "$$" > "$PID_FILE" 2>/dev/null || true

STOP=0
LAST_PID=''
LAST_START=''
LAST_STATE='unknown'
FAILS=0
FAIL_LABELS=''
FAIL_LATCH=0

trap 'STOP=1' TERM INT
trap 'rm -f "$PID_FILE" 2>/dev/null || true' EXIT

clean_line() {
    printf '%s' "$*" \
        | tr '\r\n' ' ' \
        | sed -E \
            -e 's/([Aa]uthorization[=:][[:space:]]*(Bearer[[:space:]]+)?)[^,;[:space:]]+/\1<redacted>/g' \
            -e 's/([Cc]ookie[=:][[:space:]]*)[^,;[:space:]]+/\1<redacted>/g' \
            -e 's/([Pp]assword|[Pp]asswd|[Ss]ecret|[Aa]ccess[_-]?[Tt]oken|[Rr]efresh[_-]?[Tt]oken)[=:][[:space:]]*[^,;[:space:]}]+/\1=<redacted>/g' \
            -e 's/([?&](token|access_token|refresh_token|sign|password|passwd|secret|cookie)=)[^&[:space:]]+/\1<redacted>/g' \
        | cut -c1-1200
}

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$(clean_line "$*")" | tee -a "$LOG"
}

run_readonly() {
    label=$1
    shift
    if ! command -v "$1" >/dev/null 2>&1; then
        log "READ label=$label status=command-unavailable command=$1"
        return 0
    fi
    output="$("$@" 2>&1 || true)"
    log "READ label=$label value=$(clean_line "$output")"
}

openlist_pid() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -u "$OPENLIST_USER" -x "$OPENLIST_NAME" 2>/dev/null | head -n 1
        return 0
    fi
    if command -v ps >/dev/null 2>&1; then
        ps -eo pid=,user=,comm= 2>/dev/null \
            | awk -v u="$OPENLIST_USER" -v n="$OPENLIST_NAME" '$2 == u && $3 == n {print $1; exit}'
    fi
}

process_start() {
    ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//' | head -n 1
}

ipv4_for() {
    host=$1
    if command -v getent >/dev/null 2>&1; then
        getent ahostsv4 "$host" 2>/dev/null \
            | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1}' \
            | sort -u | head -n "$MAX_IPS"
        return 0
    fi
    if command -v nslookup >/dev/null 2>&1; then
        nslookup -type=A "$host" 2>/dev/null \
            | awk '$1 == "Address:" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $2}' \
            | sort -u | head -n "$MAX_IPS"
    fi
}

dns_snapshot() {
    for host in $HOSTS; do
        ips=$(ipv4_for "$host" | paste -sd, - 2>/dev/null || true)
        log "DNS host=$host ipv4=${ips:-none}"
        if command -v nslookup >/dev/null 2>&1; then
            raw=$(nslookup "$host" 2>&1 || true)
            log "DNS_RAW host=$host value=$(clean_line "$raw")"
        fi
    done
}

route_snapshot() {
    for host in $HOSTS; do
        for ip in $(ipv4_for "$host"); do
            if command -v ip >/dev/null 2>&1; then
                route=$(ip route get "$ip" 2>&1 || true)
                log "ROUTE host=$host ip=$ip value=$(clean_line "$route")"
            fi
        done
    done
}

openlist_log_matches() {
    [ -f "$OPENLIST_LOG" ] || {
        log "OPENLIST_LOG status=missing path=$OPENLIST_LOG"
        return 0
    }
    matches=$(tail -n 160 "$OPENLIST_LOG" 2>/dev/null \
        | grep -aEi '139|route|failed init|PersonalCloudHost|EOF|unsupported|refresh|token|login|reset|timeout|connection refused' \
        | tail -n 80 2>/dev/null || true)
    log "OPENLIST_LOG path=$OPENLIST_LOG matches=$(clean_line "$matches")"
}

snapshot() {
    reason=$1
    log "SNAPSHOT_BEGIN reason=$reason"
    log "HOST date=$(date -Is 2>/dev/null || date) hostname=$(hostname 2>/dev/null || echo unknown) boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown) uptime=$(cat /proc/uptime 2>/dev/null || echo unknown)"
    run_readonly ip_addr ip -br addr
    run_readonly ip_route ip route
    run_readonly ip_rule ip rule
    route_snapshot
    run_readonly resolv_conf cat /etc/resolv.conf
    run_readonly hosts cat /etc/hosts
    run_readonly nsswitch cat /etc/nsswitch.conf
    run_readonly sockets ss -s
    run_readonly sockets_443 ss -tnp
    run_readonly memory free -h
    run_readonly loadavg cat /proc/loadavg
    run_readonly conntrack_count cat /proc/sys/net/netfilter/nf_conntrack_count
    run_readonly conntrack_max cat /proc/sys/net/netfilter/nf_conntrack_max
    run_readonly disk df -h "$DIAG_DIR"
    pid=$(openlist_pid || true)
    if [ -n "$pid" ]; then
        cmdline=$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
        rss=$(ps -o rss= -p "$pid" 2>/dev/null | head -n 1)
        threads=$(ps -o nlwp= -p "$pid" 2>/dev/null | head -n 1)
        fds=$(find "/proc/$pid/fd" -maxdepth 1 -type l 2>/dev/null | wc -l)
        log "OPENLIST pid=$pid start=$(process_start "$pid") rss_kb=$(clean_line "$rss") threads=$(clean_line "$threads") fds=$(clean_line "$fds") cmd=$(clean_line "$cmdline")"
        run_readonly openlist_cgroup cat "/proc/$pid/cgroup"
    else
        log "OPENLIST state=stopped user=$OPENLIST_USER"
    fi
    if command -v curl >/dev/null 2>&1; then
        local_http=$(curl -4 -skS --http1.1 --connect-timeout 2 --max-time 5 \
            -o /dev/null -w 'HTTP=%{http_code} IP=%{remote_ip}' \
            "http://127.0.0.1:$OPENLIST_PORT/" 2>&1 || true)
        log "LOCAL_HTTP port=$OPENLIST_PORT result=$(clean_line "$local_http")"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        run_readonly appcenter systemctl show trim_app_center.service -p MainPID -p ActiveState -p SubState -p KillMode -p ActiveEnterTimestamp
    fi
    if command -v journalctl >/dev/null 2>&1; then
        run_readonly kernel_events sh -c "journalctl -k -n 80 --no-pager 2>/dev/null | grep -Ei 'oom|killed process|conntrack|drop|reset|nf_' | tail -n 40"
    elif command -v dmesg >/dev/null 2>&1; then
        run_readonly kernel_events sh -c "dmesg -T 2>/dev/null | grep -Ei 'oom|killed process|conntrack|drop|reset|nf_' | tail -n 40"
    fi
    dns_snapshot
    openlist_log_matches
    log "SNAPSHOT_END reason=$reason"
}

curl_probe() {
    host=$1
    method=$2
    path=$3
    ip=$4
    label=$5
    url="https://$host$path"
    err_file=$(mktemp /tmp/openlist-curl.XXXXXX 2>/dev/null || echo /tmp/openlist-curl.$$.err)
    if [ "$method" = POST ]; then
        if [ "$ip" = DNS ]; then
            result=$(curl -4 -skS --http1.1 --connect-timeout "$CONNECT_SECONDS" --max-time "$REQUEST_SECONDS" \
                -H 'Content-Type: application/json' --data '{}' -o /dev/null \
                -w 'HTTP=%{http_code};IP=%{remote_ip}' "$url" 2>"$err_file")
        else
            result=$(curl -4 -skS --http1.1 --connect-timeout "$CONNECT_SECONDS" --max-time "$REQUEST_SECONDS" \
                --resolve "$host:443:$ip" -H 'Content-Type: application/json' --data '{}' -o /dev/null \
                -w 'HTTP=%{http_code};IP=%{remote_ip}' "$url" 2>"$err_file")
        fi
    elif [ "$ip" = DNS ]; then
        result=$(curl -4 -skS --http1.1 --connect-timeout "$CONNECT_SECONDS" --max-time "$REQUEST_SECONDS" \
            -o /dev/null -w 'HTTP=%{http_code};IP=%{remote_ip}' "$url" 2>"$err_file")
    else
        result=$(curl -4 -skS --http1.1 --connect-timeout "$CONNECT_SECONDS" --max-time "$REQUEST_SECONDS" \
            --resolve "$host:443:$ip" -o /dev/null -w 'HTTP=%{http_code};IP=%{remote_ip}' "$url" 2>"$err_file")
    fi
    rc=$?
    http=$(printf '%s' "$result" | sed -n 's/.*HTTP=\([0-9][0-9][0-9]\).*/\1/p')
    error=$(cat "$err_file" 2>/dev/null | tr '\r\n' ' ' || true)
    rm -f "$err_file" 2>/dev/null || true
    if [ "$rc" -eq 0 ] && [ -n "$http" ] && [ "$http" != 000 ]; then
        status=reachable
    else
        status=failed
        FAILS=$((FAILS + 1))
        FAIL_LABELS="$FAIL_LABELS,$label/$ip"
    fi
    log "PROBE label=$label host=$host ip=$ip status=$status rc=$rc http=${http:-000} result=$(clean_line "$result") error=$(clean_line "$error")"
}

probe_hosts() {
    mode=$1
    FAILS=0
    FAIL_LABELS=''
    if [ "$mode" = transport ]; then
        log "PROBE_BEGIN mode=transport"
        for host in $HOSTS; do
            ips=$(ipv4_for "$host")
            if [ -z "$ips" ]; then
                curl_probe "$host" GET / DNS "transport-$host"
            else
                for ip in $ips; do
                    curl_probe "$host" GET / "$ip" "transport-$host"
                done
            fi
        done
    else
        log "PROBE_BEGIN mode=api"
        api_specs='route|user-njs.yun.139.com|POST|/user/route/qryRoutePolicy
personal|personal-kd-njs.yun.139.com|POST|/hcy/file/list
token-refresh|aas.caiyun.feixin.10086.cn|GET|/
token-login|mail.10086.cn|GET|/
token-settings|smsrebuild1.mail.10086.cn|GET|/'
        while IFS='|' read -r label host method path; do
            [ -n "$label" ] || continue
            ips=$(ipv4_for "$host")
            if [ -z "$ips" ]; then
                curl_probe "$host" "$method" "$path" DNS "$label"
            else
                for ip in $ips; do
                    curl_probe "$host" "$method" "$path" "$ip" "$label"
                done
            fi
        done <<EOF
$api_specs
EOF
    fi
    log "PROBE_END mode=$mode failures=$FAILS labels=${FAIL_LABELS#,}"
}

handle_probe() {
    phase=$1
    if [ "$FAILS" -gt 0 ]; then
        log "EVENT network-failure phase=$phase failures=$FAILS labels=${FAIL_LABELS#,}"
        if [ "$FAIL_LATCH" -eq 0 ]; then
            FAIL_LATCH=1
            snapshot "first-failure-$phase"
        fi
    elif [ "$FAIL_LATCH" -eq 1 ]; then
        FAIL_LATCH=0
        log "EVENT network-recovered phase=$phase"
    fi
}

log "SESSION_BEGIN pid=$$ data=$OPENLIST_DATA log=$LOG"
log "SAFETY read-only=1 no_restart=1 no_hosts_edit=1 no_dns_change=1 no_admin_api=1 credentials=not-collected"
snapshot boot-baseline
probe_hosts transport
handle_probe boot-baseline

LAST_PID=$(openlist_pid || true)
if [ -n "$LAST_PID" ]; then
    LAST_STATE=running
    LAST_START=$(process_start "$LAST_PID")
    log "EVENT openlist-already-running pid=$LAST_PID start=$LAST_START"
else
    LAST_STATE=stopped
    log "EVENT openlist-stopped waiting-for-manual-start"
fi

next_probe=$(date +%s 2>/dev/null || echo 0)
next_probe=$((next_probe + PROBE_SECONDS))
while [ "$STOP" -eq 0 ]; do
    pid=$(openlist_pid || true)
    if [ -n "$pid" ]; then
        start=$(process_start "$pid")
        if [ "$LAST_STATE" != running ] || [ "$pid" != "$LAST_PID" ] || [ "$start" != "$LAST_START" ]; then
            LAST_STATE=running
            LAST_PID=$pid
            LAST_START=$start
            log "EVENT openlist-start pid=$pid start=$start"
            snapshot openlist-start
            probe_hosts api
            handle_probe openlist-start
            sleep "$SETTLE_SECONDS" || true
            [ "$STOP" -eq 1 ] && break
            snapshot openlist-start-settled
            probe_hosts api
            handle_probe openlist-start-settled
            next_probe=$(date +%s 2>/dev/null || echo 0)
            next_probe=$((next_probe + PROBE_SECONDS))
        fi
    elif [ "$LAST_STATE" = running ]; then
        log "EVENT openlist-stop previous_pid=$LAST_PID"
        snapshot openlist-stop
        LAST_STATE=stopped
        LAST_PID=''
        LAST_START=''
    fi

    now=$(date +%s 2>/dev/null || echo 0)
    if [ "$now" -ge "$next_probe" ]; then
        if [ "$LAST_STATE" = running ]; then
            probe_hosts api
        else
            probe_hosts transport
        fi
        handle_probe periodic
        next_probe=$((now + PROBE_SECONDS))
    fi
    sleep "$POLL_SECONDS" || true
done

log "SESSION_END reason=signal"
