from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
import json
from pathlib import Path
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .vk_board import build_topic_url
from .vk_dry_run import TopicKind, VkComment, VkTopicDryRunReport, parse_api_topic_result


VK_API_BASE_URL = "https://api.vk.com/method"
DEFAULT_VK_API_VERSION = "5.199"


class VkApiError(RuntimeError):
    """A read-only VK API request could not be completed."""


class VkApiNotConnectedError(VkApiError):
    """BruceBet has no server-side VK user token yet."""


@dataclass(frozen=True)
class VkApiTopic:
    group_id: int
    topic_id: int
    title: str
    comments: tuple[VkComment, ...]


def load_server_access_token(path: str | Path) -> str:
    credential_path = Path(path)
    try:
        payload = json.loads(credential_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VkApiNotConnectedError("VK OAuth is not connected") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VkApiNotConnectedError("VK OAuth credentials are unreadable") from exc

    token = str(payload.get("access_token", "")).strip()
    if not token:
        raise VkApiNotConnectedError("VK OAuth credentials contain no access token")
    return token


def _plain_lines(value: str) -> tuple[str, ...]:
    text = unescape(value or "")
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:div|p|li|blockquote)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _display_name(item: dict[str, object]) -> str:
    if str(item.get("type", "")) == "group":
        return str(item.get("name", "")).strip() or f"group {item.get('id', '?')}"
    first_name = str(item.get("first_name", "")).strip()
    last_name = str(item.get("last_name", "")).strip()
    return " ".join(part for part in (first_name, last_name) if part) or f"id{item.get('id', '?')}"


class VkApiClient:
    """Minimal read-only client for the two configured Forecasters Club topics."""

    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = DEFAULT_VK_API_VERSION,
        timeout: int = 20,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        if not access_token.strip():
            raise ValueError("VK access token is required")
        self.access_token = access_token.strip()
        self.api_version = api_version.strip() or DEFAULT_VK_API_VERSION
        self.timeout = max(5, int(timeout))
        self._opener = opener

    def _call(self, method: str, **params: object) -> dict[str, object]:
        encoded = urlencode(
            {
                **{key: str(value) for key, value in params.items() if value is not None},
                "access_token": self.access_token,
                "v": self.api_version,
            }
        )
        request = Request(f"{VK_API_BASE_URL}/{method}?{encoded}", headers={"User-Agent": "BruceBet/3000"})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise VkApiError(f"VK API {method} request failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise VkApiError(f"VK API {method} returned an unexpected payload")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("error_code", "?")
            message = str(error.get("error_msg", "unknown VK API error"))
            raise VkApiError(f"VK API {method} error {code}: {message}")
        response = payload.get("response")
        if not isinstance(response, dict):
            raise VkApiError(f"VK API {method} returned no response")
        return response

    def topic_comments(self, group_id: int, topic_id: int) -> tuple[VkComment, ...]:
        offset = 0
        ordinal = 0
        resolved_names: dict[int, str] = {}
        comments: list[VkComment] = []

        while True:
            response = self._call(
                "board.getComments",
                group_id=int(group_id),
                topic_id=int(topic_id),
                need_likes=0,
                extended=1,
                count=100,
                offset=offset,
            )
            profiles = response.get("profiles", [])
            groups = response.get("groups", [])
            if not isinstance(profiles, list) or not isinstance(groups, list):
                raise VkApiError("VK API board.getComments returned invalid authors")
            for item in profiles:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("id")
                if isinstance(raw_id, int):
                    resolved_names[raw_id] = _display_name(item)
            for item in groups:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("id")
                if isinstance(raw_id, int):
                    resolved_names[-raw_id] = _display_name({**item, "type": "group"})

            items = response.get("items", [])
            if not isinstance(items, list):
                raise VkApiError("VK API board.getComments returned invalid items")
            for item in items:
                if not isinstance(item, dict):
                    continue
                comment_id = item.get("id")
                from_id = item.get("from_id")
                timestamp = item.get("date")
                if not isinstance(comment_id, int) or not isinstance(from_id, int) or not isinstance(timestamp, int):
                    continue
                ordinal += 1
                submitted_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                comments.append(
                    VkComment(
                        source_key=f"vk-api:{int(group_id)}:{int(topic_id)}:{comment_id}",
                        author=resolved_names.get(from_id, f"id{from_id}"),
                        submitted_at=submitted_at,
                        source_line=ordinal,
                        body_lines=_plain_lines(str(item.get("text", ""))),
                    )
                )

            offset += len(items)
            total = response.get("count", offset)
            if not isinstance(total, int) or not items or offset >= total:
                break
        return tuple(comments)


def read_api_topic_dry_run(
    *,
    credentials_path: str | Path,
    group_id: int,
    topic_id: int,
    topic_kind: TopicKind,
    title: str,
    api_version: str = DEFAULT_VK_API_VERSION,
    timeout: int = 20,
    client_factory: Callable[..., VkApiClient] = VkApiClient,
) -> VkTopicDryRunReport:
    client = client_factory(
        load_server_access_token(credentials_path),
        api_version=api_version,
        timeout=timeout,
    )
    comments = client.topic_comments(group_id, topic_id)
    return parse_api_topic_result(
        group_id=group_id,
        topic_id=topic_id,
        title=title,
        topic_kind=topic_kind,
        comments=comments,
        url=build_topic_url(group_id, topic_id),
    )
