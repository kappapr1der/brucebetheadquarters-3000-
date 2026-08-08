from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable


DEFAULT_FORECASTERS_GROUP_ID = 217130885
DEFAULT_CHROMIUM_BIN = "chromium"
DEFAULT_BROWSER_WAIT_MS = 8000
TOPIC_URL_RE = re.compile(
    r"^https?://(?:m\.)?(?:vk\.com|vk\.ru)/topic-(?P<group>\d+)_(?P<topic>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
SCORE_LINE_RE = re.compile(r".+\s+\d+\s*[:;\-]\s*\d+\s*$")


class VkBrowserError(RuntimeError):
    pass


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


@dataclass(frozen=True)
class VkPublicTopicResult:
    group_id: int
    topic_id: int
    url: str
    html_chars: int
    visible_chars: int
    score_line_count: int
    text: str

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line.strip()]


def parse_topic_url(url: str) -> tuple[int, int]:
    match = TOPIC_URL_RE.match(url.strip())
    if not match:
        raise ValueError("Expected VK topic URL like https://vk.ru/topic-217130885_12345678")
    return int(match.group("group")), int(match.group("topic"))


def build_topic_url(group_id: int, topic_id: int) -> str:
    return f"https://vk.ru/topic-{int(group_id)}_{int(topic_id)}"


def extract_visible_text(html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(html)
    return unescape("\n".join(parser.parts))


def chromium_command(
    url: str,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    virtual_time_ms: int = DEFAULT_BROWSER_WAIT_MS,
) -> list[str]:
    return [
        chromium_bin,
        "--headless",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--no-first-run",
        "--lang=ru-RU",
        f"--virtual-time-budget={max(1000, int(virtual_time_ms))}",
        "--dump-dom",
        url,
    ]


def fetch_topic_html(
    url: str,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    virtual_time_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
    runner: Callable[..., object] = subprocess.run,
) -> str:
    command = chromium_command(url, chromium_bin=chromium_bin, virtual_time_ms=virtual_time_ms)
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise VkBrowserError(f"Chromium executable not found: {chromium_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VkBrowserError(f"VK Chromium probe timed out after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001 - CLI should report a compact browser failure.
        raise VkBrowserError(f"VK Chromium probe failed: {exc}") from exc

    returncode = int(getattr(completed, "returncode", 1))
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if returncode != 0:
        detail = " | ".join(line.strip() for line in stderr.splitlines()[-4:] if line.strip())
        raise VkBrowserError(f"Chromium exited with code {returncode}: {detail or 'no stderr'}")
    if not stdout.strip():
        raise VkBrowserError("Chromium returned an empty VK page")
    return stdout


def probe_public_topic(
    group_id: int,
    topic_id: int,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    virtual_time_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
    runner: Callable[..., object] = subprocess.run,
) -> VkPublicTopicResult:
    url = build_topic_url(group_id, topic_id)
    html = fetch_topic_html(
        url,
        chromium_bin=chromium_bin,
        virtual_time_ms=virtual_time_ms,
        timeout=timeout,
        runner=runner,
    )
    text = extract_visible_text(html)
    score_lines = sum(1 for line in text.splitlines() if SCORE_LINE_RE.fullmatch(line.strip()))
    return VkPublicTopicResult(
        group_id=int(group_id),
        topic_id=int(topic_id),
        url=url,
        html_chars=len(html),
        visible_chars=len(text),
        score_line_count=score_lines,
        text=text,
    )


def _env_int(name: str, fallback: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    return int(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read a public Forecasters Club VK topic through headless Chromium.")
    parser.add_argument("--topic-url", help="VK topic URL; overrides --group-id and --topic-id.")
    parser.add_argument("--group-id", type=int, default=_env_int("VK_GROUP_ID", DEFAULT_FORECASTERS_GROUP_ID))
    parser.add_argument("--topic-id", type=int, default=_env_int("VK_PREDICTIONS_TOPIC_ID"))
    parser.add_argument("--chromium", default=os.getenv("VK_CHROMIUM_BIN", DEFAULT_CHROMIUM_BIN).strip())
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=_env_int("VK_BROWSER_WAIT_MS", DEFAULT_BROWSER_WAIT_MS),
        help="Virtual time budget for VK JavaScript rendering.",
    )
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--show-lines", type=int, default=120)
    parser.add_argument("--html-out", type=Path)
    parser.add_argument("--text-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    group_id = args.group_id
    topic_id = args.topic_id
    if args.topic_url:
        try:
            group_id, topic_id = parse_topic_url(args.topic_url)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if not group_id or not topic_id:
        print(
            "VK group/topic is not configured. Pass --topic-url or set VK_GROUP_ID and VK_PREDICTIONS_TOPIC_ID.",
            file=sys.stderr,
        )
        return 2

    try:
        url = build_topic_url(group_id, topic_id)
        html = fetch_topic_html(
            url,
            chromium_bin=args.chromium,
            virtual_time_ms=args.wait_ms,
            timeout=args.timeout,
        )
        text = extract_visible_text(html)
        result = VkPublicTopicResult(
            group_id=int(group_id),
            topic_id=int(topic_id),
            url=url,
            html_chars=len(html),
            visible_chars=len(text),
            score_line_count=sum(1 for line in text.splitlines() if SCORE_LINE_RE.fullmatch(line.strip())),
            text=text,
        )
    except (ValueError, VkBrowserError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.html_out:
        args.html_out.parent.mkdir(parents=True, exist_ok=True)
        args.html_out.write_text(html, encoding="utf-8")
    if args.text_out:
        args.text_out.parent.mkdir(parents=True, exist_ok=True)
        args.text_out.write_text(result.text + "\n", encoding="utf-8")

    print(f"VK group: {result.group_id}")
    print(f"VK topic: {result.topic_id}")
    print(f"URL: {result.url}")
    print(f"HTML chars: {result.html_chars}")
    print(f"Visible chars: {result.visible_chars}")
    print(f"Prediction-like score lines: {result.score_line_count}")
    print(f"Forecasters Club visible: {'yes' if 'Forecasters Club' in result.text else 'no'}")

    lines = result.lines
    if args.show_lines > 0 and lines:
        print()
        print("Visible topic text:")
        for line in lines[: args.show_lines]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
