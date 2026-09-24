#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat


ROOT = Path("/var/lib/brucebet-vk-pull/inbox")
ARCHIVE = ROOT / "archive"
LATEST = ROOT / "latest-complete"
KEEP = 30


def validated_run(path: Path) -> tuple[Path, int, dict[str, int]]:
    archive = ARCHIVE.resolve(strict=True)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"archive run disappeared: {path.name}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"unsafe archive run: {path.name}")
    resolved = path.resolve(strict=True)
    if resolved.parent != archive:
        raise RuntimeError(f"archive run escaped archive: {path.name}")

    child_modes: dict[str, int] = {}
    for child in resolved.iterdir():
        child_info = child.lstat()
        if not stat.S_ISREG(child_info.st_mode):
            raise RuntimeError(f"unsafe archive payload: {path.name}/{child.name}")
        child_modes[child.name] = stat.S_IMODE(child_info.st_mode)
    return resolved, stat.S_IMODE(info.st_mode), child_modes


def reseal_survivors(path: Path, directory_mode: int, child_modes: dict[str, int]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return
    for name, mode in child_modes.items():
        child = path / name
        try:
            child_info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(child_info.st_mode):
            os.chmod(child, mode, follow_symlinks=False)
    os.chmod(path, directory_mode, follow_symlinks=False)


def remove_run(path: Path) -> None:
    resolved, directory_mode, child_modes = validated_run(path)
    os.chmod(resolved, directory_mode | stat.S_IWUSR, follow_symlinks=False)
    try:
        shutil.rmtree(resolved)
    except BaseException:
        reseal_survivors(resolved, directory_mode, child_modes)
        raise


def main() -> int:
    protected: str | None = None
    try:
        resolved = LATEST.resolve(strict=True)
        if resolved.parent != ARCHIVE.resolve(strict=True):
            raise RuntimeError("latest-complete escaped archive")
        protected = resolved.name
    except FileNotFoundError:
        pass
    runs = sorted(
        (path for path in ARCHIVE.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    keep = {path.name for path in runs[:KEEP]}
    if protected:
        keep.add(protected)
    removed: list[str] = []
    for path in runs:
        if path.name in keep:
            continue
        remove_run(path)
        removed.append(path.name)
    print(json.dumps({"schema": "brucebet.vk-pull-retention/v1", "kept": sorted(keep), "removed": removed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
