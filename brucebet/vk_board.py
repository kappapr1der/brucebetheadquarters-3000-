from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import sys
from typing import Callable
import urllib.parse
import urllib.request


VK_API_BASE = "https://api.vk.com/method"
DEFAULT_VK_API_VERSION = "5.199"
DEFAULT_FORECASTERS_GROUP_ID = 217130885
TOPIC_URL_RE = re.compile(
    r"^https?://(?:m\.)?(?:vk\.com|vk\.ru)/topic-(?P<group>\d+)_(?P<topic>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


class VkApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class VkBoardComment:
    comment_id: int
    from_id: int
    author_name: str
    date: datetime | None
    text: str


@dataclass(frozen=True)
class VkBoardProbeResult:
    group_id: int
    topic_id: int
    total_count: int
    fetched: int
    comments: list[VkBoardComment]

    @property
    def author_count(self) -> int:
        return len({item.from_id for item in self.comments})


def parse_topic_url(url: str) -> tuple[int, int]:
    match = TOPIC_URL_RE.match(url.strip())
    if not match:
        raise ValueError("Expected VK topic URL like https://vk.ru/topic-217130885_12345678")
    return int(match.group("group")), int(match.group("topic"))


def _profile_name(item: dict[str, object]) -> str:
    first = str(item.get("first_name") or "").strip()
    last = str(item.get("last_name") or "").strip()
    name = " ".join(part for part in (first, last) if part)
    return name or str(item.get("name") or "").strip()


def _author_map(payload: dict[str, object]) -> dict[int, str]:
    authors: dict[int, str] = {}
    for item in payload.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            authors[int(item["id"])] = _profile_name(item) or f"VK user {item['id']}"
    for item in payload.get("groups") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            group_id = int(item["id"])
            authors[-group_id] = str(item.get("name") or f"VK group {group_id}").strip()
    return authors


def _comment_from_api(item: dict[str, object], authors: dict[int, str]) -> VkBoardComment:
    from_id = int(item.get("from_id") or 0)
    raw_date = item.get("date")
    date = datetime.fromtimestamp(float(raw_date), timezone.utc) if raw_date is not None else None
    return VkBoardComment(
        comment_id=int(item.get("id") or 0),
        from_id=from_id,
        author_name=authors.get(from_id, f"VK {from_id}"),
        date=date,
        text=str(item.get("text") or "").strip(),
    )


class VkBoardClient:
    def __init__(
        self,
        access_token: str,
        api_version: str = DEFAULT_VK_API_VERSION,
        base_url: str = VK_API_BASE,
        timeout: int = 20,
        opener: Callable[..., object] | None = None,
    ) -> None:
        token = access_token.strip()
        if not token:
            raise ValueError("VK access token is empty")
        self.access_token = token
        self.api_version = api_version.strip() or DEFAULT_VK_API_VERSION
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def _get(self, method: str, params: dict[str, object]) -> dict[str, object]:
        query = {
            **params,
            "access_token": self.access_token,
            "v": self.api_version,
        }
        url = f"{self.base_url}/{method}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "BruceBetHQ/0.1 VK read-only probe",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - compact CLI diagnostics are intentional.
            raise VkApiError(f"VK API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise VkApiError("VK API returned an unexpected payload")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("error_code")
            message = error.get("error_msg") or "unknown VK API error"
            raise VkApiError(f"VK API error {code}: {message}")
        response_payload = payload.get("response")
        if not isinstance(response_payload, dict):
            raise VkApiError("VK API response object is missing")
        return response_payload

    def get_comments_page(
        self,
        group_id: int,
        topic_id: int,
        *,
        offset: int = 0,
        count: int = 100,
        sort: str = "asc",
    ) -> dict[str, object]:
        return self._get(
            "board.getComments",
            {
                "group_id": int(group_id),
                "topic_id": int(topic_id),
                "offset": max(0, int(offset)),
                "count": max(1, min(int(count), 100)),
                "sort": sort,
                "extended": 1,
            },
        )

    def probe_topic(self, group_id: int, topic_id: int, limit: int = 100) -> VkBoardProbeResult:
        wanted = max(1, int(limit))
        comments: list[VkBoardComment] = []
        offset = 0
        total_count = 0
        while len(comments) < wanted:
            page_size = min(100, wanted - len(comments))
            payload = self.get_comments_page(group_id, topic_id, offset=offset, count=page_size)
            total_count = int(payload.get("count") or 0)
            authors = _author_map(payload)
            items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
            comments.extend(_comment_from_api(item, authors) for item in items)
            if not items or len(comments) >= total_count:
                break
            offset += len(items)
        return VkBoardProbeResult(
            group_id=int(group_id),
            topic_id=int(topic_id),
            total_count=total_count,
            fetched=len(comments),
            comments=comments,
        )


def _env_int(name: str, fallback: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    return int(raw)


def _preview(value: str, limit: int = 140) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only VK discussion topic probe for BruceBet.")
    parser.add_argument("--topic-url", help="VK topic URL; overrides --group-id and --topic-id.")
    parser.add_argument("--group-id", type=int, default=_env_int("VK_GROUP_ID", DEFAULT_FORECASTERS_GROUP_ID))
    parser.add_argument("--topic-id", type=int, default=_env_int("VK_PREDICTIONS_TOPIC_ID"))
    parser.add_argument("--access-token", default=os.getenv("VK_ACCESS_TOKEN", "").strip())
    parser.add_argument("--api-version", default=os.getenv("VK_API_VERSION", DEFAULT_VK_API_VERSION).strip())
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--limit", type=int, default=100, help="Maximum comments to fetch during the probe.")
    parser.add_argument("--show", type=int, default=10, help="How many fetched comments to print.")
    parser.add_argument("--json", action="store_true", help="Print fetched comments as JSON.")
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
    if not args.access_token:
        print(
            "VK_ACCESS_TOKEN is not set. The read-only probe is ready, but VK authorization is still required.",
            file=sys.stderr,
        )
        return 2

    try:
        result = VkBoardClient(
            args.access_token,
            api_version=args.api_version,
            timeout=args.timeout,
        ).probe_topic(group_id, topic_id, limit=args.limit)
    except (ValueError, VkApiError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "group_id": result.group_id,
                    "topic_id": result.topic_id,
                    "total_count": result.total_count,
                    "fetched": result.fetched,
                    "authors": result.author_count,
                    "comments": [
                        {
                            "id": item.comment_id,
                            "from_id": item.from_id,
                            "author": item.author_name,
                            "date": item.date.isoformat() if item.date else None,
                            "text": item.text,
                        }
                        for item in result.comments
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"VK group: {result.group_id}")
    print(f"VK topic: {result.topic_id}")
    print(f"API version: {args.api_version}")
    print(f"Comments in topic: {result.total_count}")
    print(f"Fetched: {result.fetched}")
    print(f"Unique authors in fetched slice: {result.author_count}")
    if result.comments and args.show > 0:
        print()
        print("Sample comments:")
        for item in result.comments[: args.show]:
            stamp = item.date.isoformat() if item.date else "unknown-time"
            print(
                f"- #{item.comment_id} | {item.author_name} ({item.from_id}) | {stamp} | {_preview(item.text)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
