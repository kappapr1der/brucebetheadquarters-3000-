#!/usr/bin/env bash
set -euo pipefail

USER_NAME=brucebet-browser
HOME_DIR=/var/lib/brucebet-browser
PROFILE_DIR=${HOME_DIR}/profile
ARTIFACT_DIR=${HOME_DIR}/poc1
DISPLAY_NUMBER=:99
CDP_PORT=9222
MIN_AVAILABLE_KIB=$((2 * 1024 * 1024))
MAX_LOAD1_MILLI=1800
MAX_CPU_PERCENT=80

if [[ ${EUID} -ne 0 ]]; then
  echo "diagnostic must run as root" >&2
  exit 1
fi

cleanup() {
  pkill -TERM -u "${USER_NAME}" -f "${PROFILE_DIR}" 2>/dev/null || true
  pkill -TERM -u "${USER_NAME}" -x Xvfb 2>/dev/null || true
  sleep 2
  pkill -KILL -u "${USER_NAME}" -f "${PROFILE_DIR}" 2>/dev/null || true
  pkill -KILL -u "${USER_NAME}" -x Xvfb 2>/dev/null || true
}
trap cleanup EXIT

if pgrep -u "${USER_NAME}" -x google-chrome >/dev/null 2>&1 || \
   pgrep -u "${USER_NAME}" -x chrome >/dev/null 2>&1 || \
   pgrep -u "${USER_NAME}" -x Xvfb >/dev/null 2>&1; then
  echo "refusing diagnostic: browser user already has GUI processes" >&2
  exit 1
fi

if ss -H -ltn "sport = :${CDP_PORT}" | grep -q .; then
  echo "refusing diagnostic: CDP port already in use" >&2
  exit 1
fi

mkdir -p "${ARTIFACT_DIR}"
install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0700 "${HOME_DIR}/runtime"
: > "${ARTIFACT_DIR}/admission.tsv"
printf 'timestamp_utc\tmemavailable_kib\tcgroup_usage_bytes\tcgroup_limit_bytes\tfailcnt\tunder_oom\tload1\tcpu_percent\n' \
  >> "${ARTIFACT_DIR}/admission.tsv"

memory_snapshot() {
  available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
    cgroup_dir="/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup | head -n 1)"
    usage=$(cat "${cgroup_dir}/memory.current")
    raw_limit=$(cat "${cgroup_dir}/memory.max")
    [[ ${raw_limit} == max ]] && limit=$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo) || limit=${raw_limit}
    failcnt=$(awk '$1 ~ /^(max|oom|oom_kill|oom_group_kill)$/ {sum += $2} END {print sum + 0}' "${cgroup_dir}/memory.events")
    under_oom=0
  else
    usage=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes)
    limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)
    failcnt=$(cat /sys/fs/cgroup/memory/memory.failcnt)
    under_oom=$(awk '$1 == "under_oom" {print $2}' /sys/fs/cgroup/memory/memory.oom_control)
  fi
}

memory_snapshot
start_failcnt=${failcnt}
for _ in 1 2 3 4 5 6; do
  read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ < /proc/stat
  cpu_total_before=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))
  cpu_idle_before=$((cpu_idle + cpu_iowait))
  sleep 2
  read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ < /proc/stat
  cpu_total_after=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))
  cpu_idle_after=$((cpu_idle + cpu_iowait))
  cpu_delta_total=$((cpu_total_after - cpu_total_before))
  cpu_delta_idle=$((cpu_idle_after - cpu_idle_before))
  cpu_percent=$((100 * (cpu_delta_total - cpu_delta_idle) / cpu_delta_total))
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  memory_snapshot
  load1=$(awk '{print $1}' /proc/loadavg)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${timestamp}" "${available}" "${usage}" "${limit}" "${failcnt}" "${under_oom}" "${load1}" "${cpu_percent}" \
    >> "${ARTIFACT_DIR}/admission.tsv"

  load1_milli=$(awk -v value="${load1}" 'BEGIN {printf "%d", value * 1000}')
  if (( available < MIN_AVAILABLE_KIB )); then
    echo "admission failed: MemAvailable below 2 GiB" >&2
    exit 1
  fi
  if (( limit - usage < 1610612736 )); then
    echo "admission failed: cgroup headroom below 1.5 GiB" >&2
    exit 1
  fi
  if (( failcnt != start_failcnt )) || [[ ${under_oom} != 0 ]]; then
    echo "admission failed: memory fail/oom signal" >&2
    exit 1
  fi
  if (( load1_milli > MAX_LOAD1_MILLI )); then
    echo "admission failed: load1 above 1.8" >&2
    exit 1
  fi
  if (( cpu_percent > MAX_CPU_PERCENT )); then
    echo "admission failed: CPU above 80 percent" >&2
    exit 1
  fi
done

runuser -u "${USER_NAME}" -- env HOME="${HOME_DIR}" \
  XDG_RUNTIME_DIR="${HOME_DIR}/runtime" \
  TZ=Europe/Moscow \
  Xvfb "${DISPLAY_NUMBER}" -screen 0 1280x900x24 -nolisten tcp -ac \
  >"${ARTIFACT_DIR}/xvfb.stdout" 2>"${ARTIFACT_DIR}/xvfb.stderr" &
xvfb_pid=$!
echo "${xvfb_pid}" > "${ARTIFACT_DIR}/xvfb.pid"

for _ in $(seq 1 20); do
  if runuser -u "${USER_NAME}" -- env DISPLAY="${DISPLAY_NUMBER}" xdpyinfo >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
runuser -u "${USER_NAME}" -- env DISPLAY="${DISPLAY_NUMBER}" xdpyinfo \
  > "${ARTIFACT_DIR}/xdpyinfo.txt"

runuser -u "${USER_NAME}" -- env HOME="${HOME_DIR}" DISPLAY="${DISPLAY_NUMBER}" \
  XDG_RUNTIME_DIR="${HOME_DIR}/runtime" \
  google-chrome-stable \
    --user-data-dir="${PROFILE_DIR}" \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    --disable-component-update \
    --disable-sync \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port="${CDP_PORT}" \
    about:blank \
  >"${ARTIFACT_DIR}/chrome.stdout" 2>"${ARTIFACT_DIR}/chrome.stderr" &
chrome_launcher_pid=$!
echo "${chrome_launcher_pid}" > "${ARTIFACT_DIR}/chrome-launcher.pid"

cdp_ready=false
for _ in $(seq 1 40); do
  if curl --silent --fail "http://127.0.0.1:${CDP_PORT}/json/version" \
      > "${ARTIFACT_DIR}/cdp-version.json"; then
    cdp_ready=true
    break
  fi
  if ! kill -0 "${chrome_launcher_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done

if [[ ${cdp_ready} != true ]]; then
  echo "browser launch failed before CDP readiness" >&2
  exit 1
fi

printf 'elapsed_seconds\ttimestamp_utc\tmemavailable_kib\tcgroup_usage_bytes\tfailcnt\tunder_oom\tload1\tbrowser_rss_kib\tbrowser_cpu_percent\n' \
  > "${ARTIFACT_DIR}/runtime.tsv"
for elapsed in $(seq 0 2 60); do
  if ! curl --silent --fail "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null; then
    echo "browser/CDP died during 60 second dwell" >&2
    exit 1
  fi
  memory_snapshot
  load1=$(awk '{print $1}' /proc/loadavg)
  browser_rss=$(ps -u "${USER_NAME}" -o rss=,comm= | awk '$2 ~ /chrome/ {sum += $1} END {print sum + 0}')
  browser_cpu=$(ps -u "${USER_NAME}" -o pcpu=,comm= | awk '$2 ~ /chrome/ {sum += $1} END {printf "%.1f", sum + 0}')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${elapsed}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${available}" "${usage}" \
    "${failcnt}" "${under_oom}" "${load1}" "${browser_rss}" "${browser_cpu}" \
    >> "${ARTIFACT_DIR}/runtime.tsv"
  sleep 2
done

curl --silent --fail "http://127.0.0.1:${CDP_PORT}/json/list" \
  > "${ARTIFACT_DIR}/cdp-targets.json"
printf 'browser_pid_count=%s\n' "$(pgrep -u "${USER_NAME}" -c -f '/opt/google/chrome' || true)" \
  > "${ARTIFACT_DIR}/diagnostic-summary.txt"
printf 'xvfb_ready=true\ncdp_ready=true\nbrowser_survived_60s=true\nsandbox_flag_used=false\n' \
  >> "${ARTIFACT_DIR}/diagnostic-summary.txt"

memory_snapshot
end_failcnt=${failcnt}
if (( end_failcnt != start_failcnt )); then
  echo "diagnostic observed increased memory.failcnt" >&2
  exit 1
fi

echo DIAGNOSTIC_COMPLETE
