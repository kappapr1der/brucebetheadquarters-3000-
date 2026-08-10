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
TOPIC_HREF_RE = re.compile(
    r"(?:https?://(?:m\.)?(?:vk\.com|vk\.ru))?/?topic-(?P<group>\d+)_(?P<topic>\d+)",
    re.IGNORECASE,
)
EPL_TOPIC_RE = re.compile(r"\b(?:апл|английск\w*\s+премьер|premier\s+league)\b", re.IGNORECASE)
NON_EPL_TOPIC_RE = re.compile(r"\b(?:рпл|российск\w*\s+премьер)\b", re.IGNORECASE)
REGISTRATION_TOPIC_RE = re.compile(r"\b(?:регистрац\w*|заявк\w*|участник\w*|взнос\w*)\b", re.IGNORECASE)
PREDICTIONS_TOPIC_RE = re.compile(r"\b(?:прогноз\w*|ставк\w*)\b", re.IGNORECASE)
ACCESS_CHALLENGE_MARKERS = (
    "Проверяем, что вы не робот",
    "checking that you are not a robot",
)


class VkBrowserError(RuntimeError):
    pass


class VkAccessChallengeError(VkBrowserError):
    """VK returned an anti-bot challenge instead of the requested public page."""


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


class _TopicLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[int, int, str, str]] = []
        self._current: tuple[int, int, str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._current is not None:
            return
        values = {name.lower(): value or "" for name, value in attrs}
        href = unescape(values.get("href", ""))
        parsed = parse_topic_href(href)
        if parsed is None:
            return
        group_id, topic_id = parsed
        self._current = (group_id, topic_id, values.get("title", ""), [])

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        value = " ".join(data.split())
        if value:
            self._current[3].append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        group_id, topic_id, title_attr, parts = self._current
        title = " ".join(parts).strip() or title_attr.strip()
        self.links.append((group_id, topic_id, title, build_topic_url(group_id, topic_id)))
        self._current = None


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


@dataclass(frozen=True)
class VkDiscoveredTopic:
    group_id: int
    topic_id: int
    url: str
    title: str
    topic_kind: str
    league_hint: str

    @property
    def is_epl_candidate(self) -> bool:
        return self.league_hint == "epl" and self.topic_kind in {"registration", "predictions"}


@dataclass(frozen=True)
class VkGroupTopicsResult:
    group_id: int
    source_url: str
    html_chars: int
    visible_chars: int
    topics: tuple[VkDiscoveredTopic, ...]


def parse_topic_url(url: str) -> tuple[int, int]:
    match = TOPIC_URL_RE.match(url.strip())
    if not match:
        raise ValueError("Expected VK topic URL like https://vk.ru/topic-217130885_12345678")
    return int(match.group("group")), int(match.group("topic"))


def build_topic_url(group_id: int, topic_id: int) -> str:
    return f"https://vk.ru/topic-{int(group_id)}_{int(topic_id)}"


def build_group_topics_urls(group_id: int) -> tuple[str, ...]:
    group = int(group_id)
    return (
        f"https://vk.ru/club{group}?act=topics",
        f"https://vk.ru/club{group}",
    )


def parse_topic_href(href: str) -> tuple[int, int] | None:
    match = TOPIC_HREF_RE.search(unescape(href).strip())
    if match is None:
        return None
    return int(match.group("group")), int(match.group("topic"))


def classify_topic(title: str) -> tuple[str, str]:
    if NON_EPL_TOPIC_RE.search(title):
        league_hint = "non_epl"
    elif EPL_TOPIC_RE.search(title):
        league_hint = "epl"
    else:
        league_hint = "unknown"

    if REGISTRATION_TOPIC_RE.search(title):
        topic_kind = "registration"
    elif PREDICTIONS_TOPIC_RE.search(title):
        topic_kind = "predictions"
    else:
        topic_kind = "other"
    return topic_kind, league_hint


def extract_topic_links(html: str, group_id: int) -> tuple[VkDiscoveredTopic, ...]:
    parser = _TopicLinkParser()
    parser.feed(html)
    topics: dict[int, VkDiscoveredTopic] = {}
    for link_group_id, topic_id, title, url in parser.links:
        if link_group_id != int(group_id):
            continue
        topic_kind, league_hint = classify_topic(title)
        candidate = VkDiscoveredTopic(
            group_id=link_group_id,
            topic_id=topic_id,
            url=url,
            title=title or f"topic-{link_group_id}_{topic_id}",
            topic_kind=topic_kind,
            league_hint=league_hint,
        )
        existing = topics.get(topic_id)
        if existing is None or len(candidate.title) > len(existing.title):
            topics[topic_id] = candidate
    return tuple(sorted(topics.values(), key=lambda item: item.topic_id, reverse=True))


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


def fetch_public_html(
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
    if any(marker.casefold() in stdout.casefold() for marker in ACCESS_CHALLENGE_MARKERS):
        raise VkAccessChallengeError("VK returned an anti-bot challenge")
    return stdout


def fetch_topic_html(
    url: str,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    virtual_time_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
    runner: Callable[..., object] = subprocess.run,
) -> str:
    return fetch_public_html(
        url,
        chromium_bin=chromium_bin,
        virtual_time_ms=virtual_time_ms,
        timeout=timeout,
        runner=runner,
    )


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


def probe_public_group_topics(
    group_id: int,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    virtual_time_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
    runner: Callable[..., object] = subprocess.run,
) -> VkGroupTopicsResult:
    failures: list[str] = []
    first_success: tuple[str, str] | None = None
    access_challenge_detected = False
    for url in build_group_topics_urls(group_id):
        try:
            html = fetch_public_html(
                url,
                chromium_bin=chromium_bin,
                virtual_time_ms=virtual_time_ms,
                timeout=timeout,
                runner=runner,
            )
        except VkAccessChallengeError as exc:
            access_challenge_detected = True
            failures.append(str(exc))
            continue
        except VkBrowserError as exc:
            failures.append(str(exc))
            continue
        if first_success is None:
            first_success = (url, html)
        topics = extract_topic_links(html, group_id)
        if topics:
            return VkGroupTopicsResult(
                group_id=int(group_id),
                source_url=url,
                html_chars=len(html),
                visible_chars=len(extract_visible_text(html)),
                topics=topics,
            )

    if access_challenge_detected:
        raise VkAccessChallengeError("VK returned an anti-bot challenge while loading the public topic list")
    if first_success is None:
        detail = " | ".join(failures[-2:]) or "no successful public group page"
        raise VkBrowserError(f"VK group topic discovery failed: {detail}")
    url, html = first_success
    return VkGroupTopicsResult(
        group_id=int(group_id),
        source_url=url,
        html_chars=len(html),
        visible_chars=len(extract_visible_text(html)),
        topics=(),
    )


def _env_int(name: str, fallback: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    return int(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read a public Forecasters Club VK topic through headless Chromium.")
    parser.add_argument("--topic-url", help="VK topic URL; overrides --group-id and --topic-id.")
    parser.add_argument("--discover", action="store_true", help="List public discussion topics in the configured VK group.")
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
    if args.discover:
        if not group_id:
            print("VK group is not configured. Pass --group-id or set VK_GROUP_ID.", file=sys.stderr)
            return 2
        try:
            result = probe_public_group_topics(
                group_id,
                chromium_bin=args.chromium,
                virtual_time_ms=args.wait_ms,
                timeout=args.timeout,
            )
        except VkBrowserError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"VK group: {result.group_id}")
        print(f"Source URL: {result.source_url}")
        print(f"Topics found: {len(result.topics)}")
        for topic in result.topics:
            print(f"- [{topic.league_hint}/{topic.topic_kind}] {topic.title}: {topic.url}")
        return 0
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
