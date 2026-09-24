#!/usr/bin/env bash
set -euo pipefail

USER_NAME=brucebet-browser
HOME_DIR=/var/lib/brucebet-browser
PROFILE_DIR=${HOME_DIR}/profile
ARTIFACT_DIR=${HOME_DIR}/poc1
DISPLAY_NUMBER=:99

cleanup_on_error() {
  rc=$?
  if (( rc != 0 )); then
    pids=$(pgrep -u "${USER_NAME}" || true)
    if [[ -n ${pids} ]]; then
      kill -TERM ${pids} 2>/dev/null || true
    fi
  fi
  exit "${rc}"
}
trap cleanup_on_error EXIT

if [[ ${EUID} -ne 0 ]]; then
  echo "persistence launcher must run as root" >&2
  exit 1
fi

if pgrep -u "${USER_NAME}" >/dev/null 2>&1; then
  echo "refusing persistence launch: browser-user processes already exist" >&2
  exit 1
fi

if find "${PROFILE_DIR}" -maxdepth 1 -name 'Singleton*' -print -quit | grep -q .; then
  echo "refusing persistence launch: profile has Singleton artifacts" >&2
  exit 1
fi

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
load1=$(awk '{print $1}' /proc/loadavg)
printf 'memavailable_kib=%s\ncgroup_usage_bytes=%s\ncgroup_limit_bytes=%s\nfailcnt=%s\nunder_oom=%s\nload1=%s\n' \
  "${available}" "${usage}" "${limit}" "${failcnt}" "${under_oom}" "${load1}" \
  > "${ARTIFACT_DIR}/persistence-admission.txt"

if (( available < 2097152 )) || (( limit - usage < 1610612736 )) || [[ ${under_oom} != 0 ]]; then
  echo "persistence admission failed" >&2
  exit 1
fi

install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0700 "${HOME_DIR}/runtime"
runuser -u "${USER_NAME}" -- sh -c \
  "nohup env HOME='${HOME_DIR}' XDG_RUNTIME_DIR='${HOME_DIR}/runtime' Xvfb '${DISPLAY_NUMBER}' -screen 0 1280x900x24 -nolisten tcp -ac >'${ARTIFACT_DIR}/persistence-xvfb.log' 2>&1 < /dev/null & echo \$!" \
  > "${ARTIFACT_DIR}/persistence-xvfb.pid"

for _ in $(seq 1 30); do
  if runuser -u "${USER_NAME}" -- env DISPLAY="${DISPLAY_NUMBER}" xdpyinfo >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
runuser -u "${USER_NAME}" -- env DISPLAY="${DISPLAY_NUMBER}" xdpyinfo >/dev/null

runuser -u "${USER_NAME}" -- sh -c \
  "nohup env HOME='${HOME_DIR}' DISPLAY='${DISPLAY_NUMBER}' XDG_RUNTIME_DIR='${HOME_DIR}/runtime' TZ='Europe/Moscow' google-chrome-stable --user-data-dir='${PROFILE_DIR}' --no-first-run --no-default-browser-check --disable-component-update --disable-sync --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --window-size=1280,900 'https://vk.ru/' >'${ARTIFACT_DIR}/persistence-chrome.stdout' 2>'${ARTIFACT_DIR}/persistence-chrome.stderr' < /dev/null & echo \$!" \
  > "${ARTIFACT_DIR}/persistence-chrome.pid"

for _ in $(seq 1 60); do
  if curl --silent --fail http://127.0.0.1:9222/json/list \
      | grep -q '"type": "page"'; then
    break
  fi
  sleep 0.25
done
curl --silent --fail http://127.0.0.1:9222/json/list >/dev/null

trap - EXIT
echo PERSISTENCE_BROWSER_READY
