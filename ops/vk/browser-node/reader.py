#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SCHEMA = "brucebet.vk-browser-node-capture/v1"
GROUP_ID = 217130885
TOPIC_ID = 67251746
TOPIC_PATH = f"/topic-{GROUP_ID}_{TOPIC_ID}"
TOPIC_URL = f"https://vk.ru{TOPIC_PATH}"
ALLOWED_HOSTS = {"vk.ru", "www.vk.ru", "vk.com", "www.vk.com"}
HOME_DIR = pathlib.Path("/var/lib/brucebet-browser")
PROFILE_DIR = HOME_DIR / "profile"
RUNTIME_DIR = HOME_DIR / "runtime"
CAPTURE_ROOT = HOME_DIR / "captures"
KERNEL_PRECHECK = CAPTURE_ROOT / "runtime" / "kernel-oom-pre.json"
CDP_BASE = "http://127.0.0.1:9222"
CDP_PORT = 9222
DISPLAY = ":99"
MIN_AVAILABLE_KIB = 2 * 1024 * 1024
MIN_CGROUP_HEADROOM_BYTES = 1536 * 1024 * 1024
MAX_LOAD1 = 1.8
MAX_CPU_PERCENT = 80.0
GUARD_SAMPLES = 6
GUARD_INTERVAL_SECONDS = 2.0
MAX_LOAD_MORE_CLICKS = 30
VK_DISPLAY_ZONE = ZoneInfo("Europe/Moscow")
VISIBLE_TIME = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[\u0430-\u044f\u0451]{3})\s+(?P<year>\d{4})\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
RELATIVE_VISIBLE_TIME = re.compile(
    r"^(?P<day>\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0432\u0447\u0435\u0440\u0430)\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
MONTHS = {
    "\u044f\u043d\u0432": 1,
    "\u0444\u0435\u0432": 2,
    "\u043c\u0430\u0440": 3,
    "\u0430\u043f\u0440": 4,
    "\u043c\u0430\u044f": 5,
    "\u0438\u044e\u043d": 6,
    "\u0438\u044e\u043b": 7,
    "\u0430\u0432\u0433": 8,
    "\u0441\u0435\u043d": 9,
    "\u043e\u043a\u0442": 10,
    "\u043d\u043e\u044f": 11,
    "\u0434\u0435\u043a": 12,
}
RECORD_KEYS = {
    "group_id",
    "topic_id",
    "post_id",
    "canonical_permalink",
    "display_author",
    "public_profile_url",
    "visible_timestamp",
    "body_text",
    "edit_status",
    "observed_at",
}
REQUIRED_RECORD_VALUES = {
    "group_id",
    "topic_id",
    "post_id",
    "canonical_permalink",
    "display_author",
    "public_profile_url",
    "visible_timestamp",
    "body_text",
    "observed_at",
}


INSPECT_JS = r"""
(() => {
  const visible = el => !!(el && el.getClientRects().length);
  const allText = document.body ? document.body.innerText : '';
  const textLower = allText.toLowerCase();
  const hostOk = /^(www\.)?vk\.(ru|com)$/.test(location.hostname);
  const topicOk = hostOk && location.pathname === '/topic-217130885_67251746';
  const challenge = /captcha|challenge/.test(location.pathname.toLowerCase()) ||
    ['проверяем, что вы не робот', 'подтвердите, что вы не робот',
     'checking that you are not a robot', 'security check', 'проверка безопасности']
      .some(x => textLower.includes(x)) ||
    !!document.querySelector('iframe[src*="captcha"], [class*="Captcha"], [id*="captcha"]');
  const loginRequired = visible(document.querySelector(
    'input[type="password"], input[name="login"], input[name="phone"]'
  ));
  const accessError = [
    'доступ к обсуждению ограничен', 'доступ к этой странице ограничен',
    'обсуждение удалено', 'страница удалена', 'access denied'
  ].some(x => textLower.includes(x));

  const publicUrl = href => {
    let u;
    try { u = new URL(href, location.href); } catch { return null; }
    if (!/^(www\.)?vk\.(ru|com)$/.test(u.hostname)) return null;
    if (!/^\/[A-Za-z0-9_.-]+$/.test(u.pathname)) return null;
    return 'https://vk.ru' + u.pathname;
  };

  const records = [];
  const seen = new Set();
  for (const post of Array.from(document.querySelectorAll('.bp_post'))) {
    const dateLink = post.querySelector('.bp_date[href], a.bp_date[href], a[href*="?post="]');
    let u;
    try { u = new URL(dateLink?.getAttribute('href') || '', location.href); } catch { continue; }
    if (!/^(www\.)?vk\.(ru|com)$/.test(u.hostname) ||
        u.pathname !== '/topic-217130885_67251746') continue;
    const postRaw = u.searchParams.get('post');
    if (!postRaw || !/^\d{1,12}$/.test(postRaw)) continue;
    const postId = Number(postRaw);
    if (seen.has(postId)) continue;
    seen.add(postId);
    const authorLink = post.querySelector('.bp_author a[href], a.bp_author[href], .bp_author[href]');
    const authorNameNode = post.querySelector('.bp_author');
    const bodyNode = post.querySelector('.bp_text, .wall_reply_text, .reply_text, [data-testid="comment-text"]');
    const editedNode = post.querySelector('.bp_edited_by');
    const timestampNode = post.querySelector('.bp_date');
    records.push({
      post_id: postId,
      canonical_permalink: 'https://vk.ru/topic-217130885_67251746?post=' + postId,
      display_author: (authorNameNode?.innerText || authorLink?.innerText || '').trim(),
      public_profile_url: authorLink ? publicUrl(authorLink.getAttribute('href')) : null,
      visible_timestamp: (timestampNode?.innerText || '').trim(),
      body_text: (bodyNode?.innerText || '').replace(/\r\n?/g, '\n').trim(),
      edit_status: editedNode ? ((editedNode.innerText || '').trim() || 'edited') : 'not_observed'
    });
  }

  const links = Array.from(document.querySelectorAll('a[href]'));
  const permalinkIds = new Set();
  const navigationUrls = new Set();
  for (const a of links) {
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch { continue; }
    if (!/^(www\.)?vk\.(ru|com)$/.test(u.hostname) ||
        u.pathname !== '/topic-217130885_67251746') continue;
    const post = u.searchParams.get('post');
    if (post && /^\d{1,12}$/.test(post)) permalinkIds.add(Number(post));
    let safe = true;
    let navigates = false;
    for (const [key, value] of u.searchParams) {
      if (['offset', 'page', 'start', 'from', 'last'].includes(key) && /^\d{1,10}$/.test(value)) {
        navigates = true;
      } else if (!(key === 'act' && ['comments', 'topic'].includes(value))) {
        safe = false;
      }
    }
    if (safe && navigates) navigationUrls.add('https://vk.ru' + u.pathname + u.search);
  }

  const controls = Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(visible);
  const loadPattern = /показать (?:ещ[её]|предыдущие|следующие)|загрузить (?:ещ[её]|предыдущие)|больше комментариев|show more (?:posts|comments)|load more/i;
  const collapsedPattern = /^(?:показать полностью|читать полностью|читать далее|show full|read more)\s*$/i;
  const loadMore = controls.filter(el => el.id === 'bt_load_more' || loadPattern.test(el.innerText || ''));
  const collapsed = controls.filter(el => collapsedPattern.test((el.innerText || '').trim()));
  const summaryRaw = (document.querySelector('#bt_summary')?.innerText || '').trim();

  return {
    host_ok: hostOk,
    topic_url_reached: topicOk,
    final_origin: hostOk ? location.origin : 'non_vk_origin',
    final_path: hostOk ? location.pathname : '/redacted',
    ready_state: document.readyState,
    challenge: challenge,
    login_required: loginRequired,
    access_error: accessError,
    summary_raw: summaryRaw,
    dom_post_nodes: document.querySelectorAll('.bp_post').length,
    unique_permalink_ids: Array.from(permalinkIds).sort((a, b) => a - b),
    navigation_urls: Array.from(navigationUrls).sort(),
    visible_load_more: loadMore.length,
    visible_collapsed_bodies: collapsed.length,
    records: records.sort((a, b) => a.post_id - b.post_id)
  };
})()
"""


CLICK_JS = r"""
(() => {
  const visible = el => !!(el && el.getClientRects().length);
  const loadPattern = /показать (?:ещ[её]|предыдущие|следующие)|загрузить (?:ещ[её]|предыдущие)|больше комментариев|show more (?:posts|comments)|load more/i;
  const collapsedPattern = /^(?:показать полностью|читать полностью|читать далее|show full|read more)\s*$/i;
  const controls = Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(visible);
  const collapsed = controls.filter(el => collapsedPattern.test((el.innerText || '').trim()));
  if (collapsed.length) {
    collapsed.forEach(el => el.click());
    return { kind: 'collapsed', count: collapsed.length };
  }
  const loadMore = controls.filter(el => el.id === 'bt_load_more' || loadPattern.test(el.innerText || ''));
  if (loadMore.length) {
    loadMore[0].click();
    return { kind: 'load_more', count: 1 };
  }
  return { kind: 'none', count: 0 };
})()
"""


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_int(path):
    return int(pathlib.Path(path).read_text(encoding="ascii").strip())


def memavailable_kib():
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("memavailable_missing")


def load1():
    return float(pathlib.Path("/proc/loadavg").read_text(encoding="ascii").split()[0])


def read_key_values(path):
    values = {}
    for line in pathlib.Path(path).read_text(encoding="ascii").splitlines():
        key, value = line.split()[:2]
        values[key] = int(value)
    return values


def cgroup_v2_path():
    for line in pathlib.Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        if line.startswith("0::"):
            relative = line.split("::", 1)[1].lstrip("/")
            return pathlib.Path("/sys/fs/cgroup") / relative
    raise RuntimeError("cgroup_v2_path_missing")


def memtotal_bytes():
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("memtotal_missing")


def cpu_counters():
    fields = pathlib.Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:9]
    values = [int(value) for value in fields]
    return sum(values), values[3] + values[4]


def cpu_percent(interval):
    total_before, idle_before = cpu_counters()
    time.sleep(interval)
    total_after, idle_after = cpu_counters()
    total_delta = total_after - total_before
    idle_delta = idle_after - idle_before
    if total_delta <= 0:
        raise RuntimeError("cpu_sample_invalid")
    return 100.0 * (total_delta - idle_delta) / total_delta


def cgroup_snapshot():
    v2 = pathlib.Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    if v2:
        root = cgroup_v2_path()
        usage = read_int(root / "memory.current")
        raw_limit = (root / "memory.max").read_text(encoding="ascii").strip()
        limit = memtotal_bytes() if raw_limit == "max" else int(raw_limit)
        events = read_key_values(root / "memory.events")
        failcnt = sum(events.get(key, 0) for key in ("max", "oom", "oom_kill", "oom_group_kill"))
        under_oom = 0
        version = 2
    else:
        root = pathlib.Path("/sys/fs/cgroup/memory")
        usage = read_int(root / "memory.usage_in_bytes")
        limit = read_int(root / "memory.limit_in_bytes")
        failcnt = read_int(root / "memory.failcnt")
        under_oom = read_key_values(root / "memory.oom_control").get("under_oom", 1)
        version = 1
    return {
        "version": version,
        "usage_bytes": usage,
        "limit_bytes": limit,
        "headroom_bytes": limit - usage,
        "failcnt": failcnt,
        "under_oom": under_oom,
    }


def browser_user_gui_pids():
    own_uid = os.getuid()
    found = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="replace")
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid_line.split()[1]) != own_uid:
                continue
            comm = (entry / "comm").read_text(encoding="ascii", errors="replace").strip()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            if comm == "Xvfb" or "google-chrome" in cmdline or "/opt/google/chrome" in cmdline:
                found.append(int(entry.name))
        except (FileNotFoundError, PermissionError, StopIteration, ValueError):
            continue
    return sorted(set(found))


def summed_browser_rss_kib():
    total = 0
    own_uid = os.getuid()
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="replace")
            fields = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in status.splitlines() if ":" in line}
            if int(fields["Uid"].split()[0]) != own_uid:
                continue
            comm = fields.get("Name", "")
            if "chrome" not in comm:
                continue
            total += int(fields.get("VmRSS", "0 kB").split()[0])
        except (FileNotFoundError, PermissionError, KeyError, ValueError):
            continue
    return total


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def singleton_artifact_count():
    return sum(
        1
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
        if os.path.lexists(PROFILE_DIR / name)
    )


def load_kernel_precheck():
    try:
        data = json.loads(KERNEL_PRECHECK.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("kernel_oom_precheck_missing") from exc
    if data.get("available") is not True:
        raise RuntimeError("kernel_oom_precheck_unavailable")
    if data.get("oom_markers") != 0:
        raise RuntimeError("kernel_oom_precheck_failed")
    return data


def resource_guard():
    if browser_user_gui_pids():
        raise RuntimeError("existing_browser_user_gui_processes")
    if port_in_use(CDP_PORT):
        raise RuntimeError("cdp_port_in_use")
    if singleton_artifact_count():
        raise RuntimeError("profile_singleton_artifact_present")
    kernel = load_kernel_precheck()
    start_cgroup = cgroup_snapshot()
    samples = []
    for _ in range(GUARD_SAMPLES):
        cpu = cpu_percent(GUARD_INTERVAL_SECONDS)
        cgroup = cgroup_snapshot()
        sample = {
            "observed_at": utc_now(),
            "memavailable_kib": memavailable_kib(),
            "load1": load1(),
            "cpu_percent": round(cpu, 2),
            **cgroup,
        }
        samples.append(sample)
        if sample["memavailable_kib"] < MIN_AVAILABLE_KIB:
            raise RuntimeError("guard_memavailable_low")
        if sample["headroom_bytes"] < MIN_CGROUP_HEADROOM_BYTES:
            raise RuntimeError("guard_cgroup_headroom_low")
        if sample["failcnt"] != start_cgroup["failcnt"] or sample["under_oom"] != 0:
            raise RuntimeError("guard_memory_pressure_signal")
        if sample["load1"] > MAX_LOAD1:
            raise RuntimeError("guard_load_high")
        if sample["cpu_percent"] > MAX_CPU_PERCENT:
            raise RuntimeError("guard_cpu_high")
    return {
        "passed": True,
        "thresholds": {
            "minimum_memavailable_kib": MIN_AVAILABLE_KIB,
            "minimum_cgroup_headroom_bytes": MIN_CGROUP_HEADROOM_BYTES,
            "maximum_load1": MAX_LOAD1,
            "maximum_cpu_percent": MAX_CPU_PERCENT,
        },
        "kernel_oom_precheck": kernel,
        "samples": samples,
        "start_failcnt": start_cgroup["failcnt"],
    }


class ResourceMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.samples = []

    def run(self):
        last_total, last_idle = cpu_counters()
        while not self.stop_event.wait(1.0):
            try:
                total, idle = cpu_counters()
                delta = total - last_total
                cpu = 0.0 if delta <= 0 else 100.0 * ((delta - (idle - last_idle)) / delta)
                last_total, last_idle = total, idle
                self.samples.append({
                    "observed_at": utc_now(),
                    "memavailable_kib": memavailable_kib(),
                    "cgroup_usage_bytes": cgroup_snapshot()["usage_bytes"],
                    "load1": load1(),
                    "host_cpu_percent": round(cpu, 2),
                    "summed_browser_rss_kib": summed_browser_rss_kib(),
                })
            except Exception:
                continue

    def finish(self):
        self.stop_event.set()
        self.join(timeout=3)
        if not self.samples:
            return {"sample_count": 0}
        return {
            "sample_count": len(self.samples),
            "minimum_memavailable_kib": min(x["memavailable_kib"] for x in self.samples),
            "peak_cgroup_usage_bytes": max(x["cgroup_usage_bytes"] for x in self.samples),
            "peak_load1": max(x["load1"] for x in self.samples),
            "peak_host_cpu_percent": max(x["host_cpu_percent"] for x in self.samples),
            "peak_summed_browser_rss_kib": max(x["summed_browser_rss_kib"] for x in self.samples),
        }


def get_json(path):
    with urllib.request.urlopen(CDP_BASE + path, timeout=5) as response:
        return json.load(response)


class CDP:
    def __init__(self, target):
        import websocket

        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True
        )
        self.sequence = 0
        self.document_statuses = []

    def _observe(self, message):
        if message.get("method") != "Network.responseReceived":
            return
        params = message.get("params", {})
        if params.get("type") != "Document":
            return
        response = params.get("response", {})
        parsed = urllib.parse.urlsplit(response.get("url", ""))
        if parsed.hostname in ALLOWED_HOSTS:
            self.document_statuses.append({
                "host": parsed.hostname,
                "path": parsed.path,
                "status": response.get("status"),
            })

    def call(self, method, params=None, timeout=30):
        self.sequence += 1
        wanted = self.sequence
        self.ws.send(json.dumps({"id": wanted, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv())
            self._observe(message)
            if message.get("id") != wanted:
                continue
            if "error" in message:
                raise RuntimeError("cdp_operation_failed")
            return message.get("result", {})
        raise TimeoutError("cdp_deadline")

    def evaluate(self, expression):
        response = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in response:
            raise RuntimeError("dom_evaluation_failed")
        return response.get("result", {}).get("value")


def page_target():
    pages = [target for target in get_json("/json/list") if target.get("type") == "page"]
    if len(pages) != 1:
        raise RuntimeError("expected_one_browser_page")
    return pages[0]


def wait_for_cdp(chrome_process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if chrome_process.poll() is not None:
            raise RuntimeError("chrome_exited_before_cdp")
        try:
            get_json("/json/version")
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("cdp_not_ready")


def wait_for_page(client):
    state = {}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.evaluate(INSPECT_JS)
        if state.get("ready_state") == "complete":
            break
        time.sleep(0.5)
    time.sleep(3)
    return client.evaluate(INSPECT_JS)


def capture(client):
    load_more_count = 0
    collapsed_body_count = 0
    client.call("Network.enable")
    client.call("Runtime.enable")
    client.call("Page.enable")
    client.call("Page.navigate", {"url": TOPIC_URL})
    state = wait_for_page(client)
    if state.get("challenge"):
        return state, "access_challenge", False, load_more_count, collapsed_body_count
    if state.get("login_required"):
        return state, "session_absent", False, load_more_count, collapsed_body_count
    if state.get("access_error") or not state.get("topic_url_reached"):
        return state, "topic_access_not_proven", False, load_more_count, collapsed_body_count

    stop_reason = "capture_not_proven"
    for _ in range(MAX_LOAD_MORE_CLICKS + 20):
        before_ids = tuple(state.get("unique_permalink_ids", []))
        action = client.evaluate(CLICK_JS)
        if action.get("kind") == "none":
            break
        if action.get("kind") == "collapsed":
            collapsed_body_count += int(action.get("count", 0))
            time.sleep(0.75)
            state = client.evaluate(INSPECT_JS)
            continue
        if load_more_count >= MAX_LOAD_MORE_CLICKS:
            stop_reason = "load_more_limit_reached"
            break
        load_more_count += 1
        progressed = False
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            time.sleep(0.5)
            next_state = client.evaluate(INSPECT_JS)
            state = next_state
            if state.get("challenge"):
                return state, "access_challenge", False, load_more_count, collapsed_body_count
            next_ids = tuple(state.get("unique_permalink_ids", []))
            if next_ids != before_ids or state.get("visible_load_more", 0) == 0:
                progressed = True
                break
        if not progressed:
            stop_reason = "load_more_no_progress"
            break

    records = state.get("records", [])
    ids = state.get("unique_permalink_ids", [])
    summary_raw = state.get("summary_raw", "")
    displayed_total = int(summary_raw) if str(summary_raw).isdigit() else None
    complete_values = bool(records) and all(
        record.get(key)
        for record in records
        for key in ("canonical_permalink", "display_author", "public_profile_url", "visible_timestamp", "body_text")
    )
    if stop_reason in {"load_more_limit_reached", "load_more_no_progress"}:
        return state, stop_reason, False, load_more_count, collapsed_body_count
    if state.get("visible_load_more", 0):
        return state, "client_pagination_unproven", False, load_more_count, collapsed_body_count
    if state.get("visible_collapsed_bodies", 0):
        return state, "collapsed_bodies_remain", False, load_more_count, collapsed_body_count
    if displayed_total is None:
        return state, "displayed_total_missing", False, load_more_count, collapsed_body_count
    if not records:
        return state, "comment_records_empty", False, load_more_count, collapsed_body_count
    if len(ids) != displayed_total:
        return state, "displayed_total_mismatch", False, load_more_count, collapsed_body_count
    if len(ids) != len(set(ids)) or len(records) != len(ids) or state.get("dom_post_nodes") != len(ids):
        return state, "comment_record_mismatch", False, load_more_count, collapsed_body_count
    if not complete_values:
        return state, "comment_fields_incomplete", False, load_more_count, collapsed_body_count
    return state, "displayed_total_exhausted", True, load_more_count, collapsed_body_count


def sanitize_records(raw_records, observed_at):
    records = []
    for raw in sorted(raw_records, key=lambda item: int(item["post_id"])):
        post_id = int(raw["post_id"])
        record = {
            "group_id": GROUP_ID,
            "topic_id": TOPIC_ID,
            "post_id": post_id,
            "canonical_permalink": f"{TOPIC_URL}?post={post_id}",
            "display_author": str(raw.get("display_author") or "").strip(),
            "public_profile_url": str(raw.get("public_profile_url") or "").strip(),
            "visible_timestamp": str(raw.get("visible_timestamp") or "").strip(),
            "body_text": str(raw.get("body_text") or "").replace("\r\n", "\n").replace("\r", "\n").strip(),
            "edit_status": str(raw.get("edit_status") or "not_observed").strip(),
            "observed_at": observed_at,
        }
        records.append(record)
    return records


def verify_records(records, displayed_total=None):
    issues = []
    ids = []
    for index, record in enumerate(records):
        if set(record) != RECORD_KEYS:
            issues.append(f"record_{index}_key_allowlist_mismatch")
        missing = [key for key in REQUIRED_RECORD_VALUES if record.get(key) in (None, "")]
        if missing:
            issues.append(f"record_{index}_required_value_missing")
        try:
            post_id = int(record["post_id"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"record_{index}_post_id_invalid")
            continue
        ids.append(post_id)
        if record.get("group_id") != GROUP_ID or record.get("topic_id") != TOPIC_ID:
            issues.append(f"record_{index}_source_id_mismatch")
        if record.get("canonical_permalink") != f"{TOPIC_URL}?post={post_id}":
            issues.append(f"record_{index}_permalink_not_canonical")
        parsed = urllib.parse.urlsplit(str(record.get("public_profile_url", "")))
        if parsed.scheme != "https" or parsed.hostname != "vk.ru" or parsed.query or parsed.fragment:
            issues.append(f"record_{index}_profile_url_not_public")
        if not re.fullmatch(r"/[A-Za-z0-9_.-]+", parsed.path or ""):
            issues.append(f"record_{index}_profile_path_invalid")
    if ids != sorted(ids):
        issues.append("post_ids_not_ascending")
    if len(ids) != len(set(ids)):
        issues.append("duplicate_post_ids")
    if displayed_total is not None and len(records) != displayed_total:
        issues.append("displayed_total_mismatch")
    return issues


def normalized_visible_timestamp(value, observed_at):
    raw = str(value or "").strip()
    match = VISIBLE_TIME.fullmatch(raw)
    if match and (month := MONTHS.get(match.group("month").casefold())):
        hour, minute = (int(part) for part in match.group("time").split(":"))
        wall_time = datetime(
            int(match.group("year")), month, int(match.group("day")), hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    relative = RELATIVE_VISIBLE_TIME.fullmatch(raw)
    if relative:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        local_date = observed.astimezone(VK_DISPLAY_ZONE).date()
        if relative.group("day").casefold() == "\u0432\u0447\u0435\u0440\u0430":
            local_date -= timedelta(days=1)
        hour, minute = (int(part) for part in relative.group("time").split(":"))
        wall_time = datetime(
            local_date.year, local_date.month, local_date.day, hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    return raw


def logical_fingerprint(records):
    logical = []
    for record in records:
        item = {key: value for key, value in record.items() if key != "observed_at"}
        item["visible_timestamp"] = normalized_visible_timestamp(
            item.get("visible_timestamp"), record.get("observed_at")
        )
        logical.append(item)
    return sha256_bytes((canonical_json(logical) + "\n").encode("utf-8"))


def jsonl_bytes(records):
    return "".join(canonical_json(record) + "\n" for record in records).encode("utf-8")


def atomic_symlink(target, link_path):
    temp_link = link_path.parent / f".{link_path.name}.tmp-{os.getpid()}"
    try:
        temp_link.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target, temp_link)
    os.replace(temp_link, link_path)


def write_bundle(manifest, records, root=CAPTURE_ROOT):
    archive = root / "archive"
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = manifest["run_id"]
    final_dir = archive / run_id
    temp_dir = root / f".tmp-{run_id}-{os.getpid()}"
    if final_dir.exists() or temp_dir.exists():
        raise RuntimeError("capture_run_path_collision")
    temp_dir.mkdir(mode=0o700)
    snapshot_data = jsonl_bytes(records)
    snapshot_sha = sha256_bytes(snapshot_data)
    fingerprint = logical_fingerprint(records)
    previous_fingerprint = None
    latest_manifest = root / "latest-complete" / "manifest.json"
    try:
        previous_fingerprint = json.loads(latest_manifest.read_text(encoding="utf-8")).get("capture_fingerprint")
    except (OSError, ValueError):
        pass
    logical_event_created = bool(manifest.get("capture_complete") and fingerprint != previous_fingerprint)
    manifest.update({
        "artifact_sha256": snapshot_sha,
        "capture_fingerprint": fingerprint,
        "logical_event_created": logical_event_created,
        "previous_complete_fingerprint": previous_fingerprint,
    })
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_sha = sha256_bytes(manifest_data)
    sums = f"{snapshot_sha}  capture.jsonl\n{manifest_sha}  manifest.json\n".encode("ascii")
    (temp_dir / "capture.jsonl").write_bytes(snapshot_data)
    (temp_dir / "manifest.json").write_bytes(manifest_data)
    (temp_dir / "SHA256SUMS").write_bytes(sums)
    loaded = [json.loads(line) for line in snapshot_data.decode("utf-8").splitlines() if line]
    issues = verify_records(loaded, manifest.get("displayed_total") if manifest.get("capture_complete") else None)
    if issues or sha256_bytes((temp_dir / "capture.jsonl").read_bytes()) != snapshot_sha:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("offline_artifact_verification_failed")
    os.replace(temp_dir, final_dir)
    for path in final_dir.iterdir():
        path.chmod(0o440)
    final_dir.chmod(0o550)
    atomic_symlink(pathlib.Path("archive") / run_id, root / "latest-attempt")
    latest_updated = False
    if manifest.get("capture_complete"):
        atomic_symlink(pathlib.Path("archive") / run_id, root / "latest-complete")
        latest_updated = True
    return {
        "run_directory": str(final_dir),
        "artifact_sha256": snapshot_sha,
        "capture_fingerprint": fingerprint,
        "logical_event_created": logical_event_created,
        "latest_complete_updated": latest_updated,
    }


def cleanup_processes(client, chrome, xvfb):
    if client is not None:
        try:
            client.call("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            client.ws.close()
        except Exception:
            pass
    if chrome is not None:
        try:
            chrome.wait(timeout=6)
        except subprocess.TimeoutExpired:
            chrome.terminate()
            try:
                chrome.wait(timeout=3)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=3)
    if xvfb is not None:
        if xvfb.poll() is None:
            xvfb.terminate()
        try:
            xvfb.wait(timeout=3)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait(timeout=3)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and browser_user_gui_pids():
        time.sleep(0.25)
    remaining = browser_user_gui_pids()
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1)
    for pid in browser_user_gui_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    lock_deadline = time.monotonic() + 3
    while time.monotonic() < lock_deadline and singleton_artifact_count():
        time.sleep(0.25)


def make_run_id(started_at):
    stamp = started_at.replace("-", "").replace(":", "").replace("T", "T").replace("Z", "Z")
    return f"{stamp}-{os.getpid()}"


def run_capture():
    started_at = utc_now()
    run_id = make_run_id(started_at)
    guard = None
    monitor = None
    xvfb = None
    chrome = None
    client = None
    state = {}
    stop_reason = "capture_error_no_retry"
    capture_complete = False
    load_more_count = 0
    collapsed_body_count = 0
    error_type = None
    result_exit = 1
    CAPTURE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        guard = resource_guard()
        env = os.environ.copy()
        env.update({
            "HOME": str(HOME_DIR),
            "XDG_RUNTIME_DIR": str(RUNTIME_DIR),
            "DISPLAY": DISPLAY,
            "TZ": "Europe/Moscow",
        })
        runtime_output = CAPTURE_ROOT / "runtime"
        runtime_output.mkdir(mode=0o700, exist_ok=True)
        xvfb_log = (runtime_output / "xvfb.log").open("ab", buffering=0)
        chrome_stdout = (runtime_output / "chrome.stdout.log").open("ab", buffering=0)
        chrome_stderr = (runtime_output / "chrome.stderr.log").open("ab", buffering=0)
        xvfb = subprocess.Popen(
            ["/usr/bin/Xvfb", DISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
            env=env,
            stdout=xvfb_log,
            stderr=xvfb_log,
        )
        for _ in range(30):
            probe = subprocess.run(
                ["/usr/bin/xdpyinfo"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if probe.returncode == 0:
                break
            if xvfb.poll() is not None:
                raise RuntimeError("xvfb_exited")
            time.sleep(0.25)
        else:
            raise RuntimeError("xvfb_not_ready")
        monitor = ResourceMonitor()
        monitor.start()
        chrome = subprocess.Popen(
            [
                "/usr/bin/google-chrome-stable",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-component-update",
                "--disable-sync",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={CDP_PORT}",
                "--window-size=1280,900",
                "about:blank",
            ],
            env=env,
            stdout=chrome_stdout,
            stderr=chrome_stderr,
        )
        wait_for_cdp(chrome)
        client = CDP(page_target())
        state, stop_reason, capture_complete, load_more_count, collapsed_body_count = capture(client)
        result_exit = 0 if capture_complete else 2
    except Exception as exc:
        error_type = type(exc).__name__
        if str(exc).startswith("guard_") or str(exc) in {
            "existing_browser_user_gui_processes", "cdp_port_in_use",
            "profile_singleton_artifact_present", "kernel_oom_precheck_missing",
            "kernel_oom_precheck_unavailable", "kernel_oom_precheck_failed",
        }:
            stop_reason = str(exc)
    finally:
        runtime_stats = monitor.finish() if monitor is not None else {"sample_count": 0}
        cleanup_processes(client, chrome, xvfb)
    finished_at = utc_now()
    summary_raw = state.get("summary_raw", "") if isinstance(state, dict) else ""
    displayed_total = int(summary_raw) if str(summary_raw).isdigit() else None
    observed_at = finished_at
    records = sanitize_records(state.get("records", []), observed_at) if isinstance(state, dict) else []
    record_issues = verify_records(records, displayed_total if capture_complete else None)
    if record_issues:
        capture_complete = False
        stop_reason = "offline_record_verification_failed"
        result_exit = 2
    ids = [record["post_id"] for record in records]
    topic_documents = [
        item for item in (client.document_statuses if client is not None else [])
        if item.get("path") == TOPIC_PATH
    ]
    end_cgroup = cgroup_snapshot()
    runtime_memory_ok = bool(
        guard
        and end_cgroup["failcnt"] == guard["start_failcnt"]
        and end_cgroup["under_oom"] == 0
    )
    if not runtime_memory_ok and capture_complete:
        capture_complete = False
        stop_reason = "runtime_memory_pressure_signal"
        result_exit = 2
    singleton_count = singleton_artifact_count()
    cleanup_ok = not browser_user_gui_pids() and not port_in_use(CDP_PORT) and singleton_count == 0
    if not cleanup_ok and capture_complete:
        capture_complete = False
        stop_reason = "cleanup_not_proven"
        result_exit = 2
    final_origin = state.get("final_origin") if isinstance(state, dict) else None
    final_path = state.get("final_path") if isinstance(state, dict) else None
    final_url = f"{final_origin}{final_path}" if final_origin in {"https://vk.ru", "https://vk.com"} else None
    manifest = {
        "schema": SCHEMA,
        "run_id": run_id,
        "group_id": GROUP_ID,
        "topic_id": TOPIC_ID,
        "requested_url": TOPIC_URL,
        "started_at": started_at,
        "finished_at": finished_at,
        "challenge": bool(state.get("challenge")) if isinstance(state, dict) else None,
        "login_required": bool(state.get("login_required")) if isinstance(state, dict) else None,
        "final_url": final_url,
        "http_document_responses": client.document_statuses if client is not None else [],
        "displayed_total": displayed_total,
        "unique_canonical_post_count": len(set(ids)),
        "newest_post_id": max(ids) if ids else None,
        "newest_visible_timestamp": records[-1]["visible_timestamp"] if records else None,
        "pages_fetched": len(topic_documents),
        "load_more_count": load_more_count,
        "collapsed_body_count": collapsed_body_count,
        "dom_navigation_urls_observed": state.get("navigation_urls", []) if isinstance(state, dict) else [],
        "pagination_exhausted": bool(
            isinstance(state, dict)
            and state.get("visible_load_more") == 0
            and state.get("visible_collapsed_bodies") == 0
        ),
        "capture_complete": capture_complete,
        "stop_reason": stop_reason,
        "record_count": len(records),
        "record_verification_issues": record_issues,
        "resource_guard": guard,
        "resource_use": runtime_stats,
        "cgroup_end": end_cgroup,
        "runtime_memory_ok": runtime_memory_ok,
        "cleanup_pass": cleanup_ok,
        "singleton_artifact_count_after_cleanup": singleton_count,
        "error_type": error_type,
        "sanitization": "allowlist_only",
    }
    artifact = write_bundle(manifest, records)
    public_result = {
        "run_id": run_id,
        "capture_complete": capture_complete,
        "stop_reason": stop_reason,
        "displayed_total": displayed_total,
        "record_count": len(records),
        "newest_post_id": manifest["newest_post_id"],
        "load_more_count": load_more_count,
        "resource_guard_pass": bool(guard and guard.get("passed") and runtime_memory_ok),
        "cleanup_pass": cleanup_ok,
        **artifact,
    }
    print(json.dumps(public_result, ensure_ascii=True, sort_keys=True))
    return result_exit


def self_test():
    observed = "2026-09-15T00:00:00Z"
    records = [
        {
            "group_id": GROUP_ID,
            "topic_id": TOPIC_ID,
            "post_id": 1,
            "canonical_permalink": f"{TOPIC_URL}?post=1",
            "display_author": "Test User",
            "public_profile_url": "https://vk.ru/test.user",
            "visible_timestamp": "today at 00:00",
            "body_text": "Team A - Team B 1:0",
            "edit_status": "not_observed",
            "observed_at": observed,
        }
    ]
    assert not verify_records(records, 1)
    first = logical_fingerprint(records)
    changed_observation = [dict(records[0], observed_at="2026-09-15T00:01:00Z")]
    assert logical_fingerprint(changed_observation) == first
    absolute = [dict(
        records[0], visible_timestamp="18 \u0441\u0435\u043d 2026 \u0432 10:13",
        observed_at="2026-09-18T09:24:47Z",
    )]
    relative = [dict(
        records[0], visible_timestamp="\u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432 10:13",
        observed_at="2026-09-18T09:24:47Z",
    )]
    assert logical_fingerprint(absolute) == logical_fingerprint(relative)
    duplicate = records + [dict(records[0])]
    assert "duplicate_post_ids" in verify_records(duplicate)
    atomic_bundle_tested = os.name != "nt"
    if atomic_bundle_tested:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = {
                "schema": SCHEMA,
                "run_id": "selftest-1",
                "capture_complete": True,
                "displayed_total": 1,
            }
            result = write_bundle(manifest, records, root=root)
            assert result["logical_event_created"] is True
            assert (root / "latest-complete" / "capture.jsonl").is_file()
            manifest2 = {
                "schema": SCHEMA,
                "run_id": "selftest-2",
                "capture_complete": True,
                "displayed_total": 1,
            }
            result2 = write_bundle(manifest2, changed_observation, root=root)
            assert result2["logical_event_created"] is False
            assert result2["latest_complete_updated"] is True
            assert json.loads(
                (root / "latest-complete" / "manifest.json").read_text(encoding="utf-8")
            )["run_id"] == "selftest-2"
    print(json.dumps({
        "self_test": True,
        "schema": SCHEMA,
        "atomic_bundle_tested": atomic_bundle_tested,
    }, sort_keys=True))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else run_capture()


if __name__ == "__main__":
    sys.exit(main())
