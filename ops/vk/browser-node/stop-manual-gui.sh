#!/usr/bin/env bash
set -euo pipefail

USER_NAME=brucebet-browser

if curl --silent --fail http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  /usr/local/lib/brucebet-vk-reader/cdp_close.py || true
  for _ in $(seq 1 30); do
    curl --silent --fail http://127.0.0.1:9222/json/version >/dev/null 2>&1 || break
    sleep 0.2
  done
fi

pids=$(pgrep -u "${USER_NAME}" || true)
if [[ -n ${pids} ]]; then
  kill -TERM ${pids}
fi
sleep 3

pids=$(pgrep -u "${USER_NAME}" || true)
if [[ -n ${pids} ]]; then
  kill -KILL ${pids}
fi

if pgrep -u "${USER_NAME}" >/dev/null 2>&1; then
  echo "browser-user processes remain" >&2
  ps -u "${USER_NAME}" -o pid=,comm=
  exit 1
fi

for name in SingletonLock SingletonSocket SingletonCookie; do
  path="/var/lib/brucebet-browser/profile/${name}"
  [[ -L ${path} ]] || continue
  target=$(readlink "${path}")
  case "${name}:${target}" in
    SingletonLock:*)
      lock_pid=${target##*-}
      [[ ${lock_pid} =~ ^[0-9]+$ ]] && ! kill -0 "${lock_pid}" 2>/dev/null && rm -- "${path}"
      ;;
    SingletonSocket:*)
      if [[ ${target} == /tmp/com.google.Chrome.*/SingletonSocket ]] \
          && ! ss -lxnp | grep -F -- "${target}" >/dev/null \
          && ! fuser "${target}" >/dev/null 2>&1; then
        rm -- "${path}"
        if [[ -S ${target} ]]; then
          temp_dir=${target%/SingletonSocket}
          rm -- "${target}"
          [[ -L ${temp_dir}/SingletonCookie ]] && rm -- "${temp_dir}/SingletonCookie"
          rmdir -- "${temp_dir}" 2>/dev/null || true
        fi
      fi
      ;;
    SingletonCookie:*)
      rm -- "${path}"
      ;;
  esac
done

if find /var/lib/brucebet-browser/profile -maxdepth 1 -name 'Singleton*' -print -quit | grep -q .; then
  echo "profile Singleton artifacts remain" >&2
  exit 1
fi

echo MANUAL_GUI_STOPPED_PROFILE_PRESERVED
