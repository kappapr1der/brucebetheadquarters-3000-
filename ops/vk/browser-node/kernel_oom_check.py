#!/usr/bin/env python3
import json
import os
import pathlib
import pwd
import re
import subprocess
import sys
import time


OUTPUT_DIR = pathlib.Path("/var/lib/brucebet-browser/captures/runtime")
PATTERN = re.compile(r"out of memory|oom-kill|killed process", re.IGNORECASE)


def main():
    phase = sys.argv[1] if len(sys.argv) == 2 else "invalid"
    if phase not in {"pre", "post"}:
        raise SystemExit(2)
    completed = subprocess.run(
        ["journalctl", "-k", "--since", "-5 min", "--no-pager", "-n", "200", "-q"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    markers = PATTERN.findall(completed.stdout) if completed.returncode == 0 else []
    result = {
        "schema": "brucebet.vk-browser-node-kernel-oom-check/v1",
        "phase": phase,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window": "5 minutes",
        "line_limit": 200,
        "available": completed.returncode == 0,
        "oom_markers": len(markers),
        "journal_exit_code": completed.returncode,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = OUTPUT_DIR / f"kernel-oom-{phase}.json"
    temp = OUTPUT_DIR / f".{target.name}.tmp-{os.getpid()}"
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    account = pwd.getpwnam("brucebet-browser")
    os.chown(temp, account.pw_uid, account.pw_gid)
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    print(json.dumps(result, sort_keys=True))
    if not result["available"] or result["oom_markers"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
