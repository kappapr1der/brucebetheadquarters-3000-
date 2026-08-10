from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from brucebet.vk_api import VkApiClient, VkApiError, load_server_access_token
from brucebet.vk_dry_run import VkComment, parse_api_topic_result
from brucebet.vk_oauth import (
    VkOAuthConfigurationError,
    VkOAuthSettings,
    complete_authorization,
    complete_worker_relay_authorization,
    create_authorization_url,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeBytesResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class VkApiTests(unittest.TestCase):
    def test_reads_and_normalizes_board_comments(self) -> None:
        payload = {
            "response": {
                "count": 1,
                "items": [{"id": 77, "from_id": 42, "date": 1780000000, "text": "Bruce Wayne<br>500 руб."}],
                "profiles": [{"id": 42, "first_name": "Bruce", "last_name": "Wayne"}],
                "groups": [],
            }
        }
        client = VkApiClient("test-token", opener=lambda *_args, **_kwargs: FakeResponse(payload))

        comments = client.topic_comments(217130885, 67251857)

        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].source_key, "vk-api:217130885:67251857:77")
        self.assertEqual(comments[0].author, "Bruce Wayne")
        self.assertEqual(comments[0].body_lines, ("Bruce Wayne", "500 руб."))
        self.assertEqual(comments[0].submitted_at.tzinfo, timezone.utc)

    def test_surfaces_vk_api_error_without_echoing_token(self) -> None:
        client = VkApiClient(
            "do-not-leak",
            opener=lambda *_args, **_kwargs: FakeResponse({"error": {"error_code": 1051, "error_msg": "Method unavailable"}}),
        )

        with self.assertRaisesRegex(VkApiError, "1051") as error:
            client.topic_comments(217130885, 67251857)
        self.assertNotIn("do-not-leak", str(error.exception))

    def test_api_parser_keeps_stable_comment_key(self) -> None:
        comment = VkComment(
            source_key="vk-api:217130885:67251857:77",
            author="Bruce Wayne",
            submitted_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            source_line=1,
            body_lines=("Bruce Wayne", "без взноса"),
        )
        report = parse_api_topic_result(
            group_id=217130885,
            topic_id=67251857,
            url="https://vk.ru/topic-217130885_67251857",
            title="Premier League registration",
            topic_kind="registration",
            comments=(comment,),
        )

        self.assertEqual(report.league_hint, "epl")
        self.assertEqual(report.registration_entries[0].source_key, comment.source_key)
        self.assertEqual(report.registration_entries[0].participant, "Bruce Wayne")

    def test_credentials_file_requires_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "no access token"):
                load_server_access_token(path)


class VkOAuthTests(unittest.TestCase):
    def test_authorization_code_flow_stores_token_without_returning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = VkOAuthSettings(
                client_id="54715552",
                client_secret="server-secret",
                redirect_uri="https://example.test/vk/oauth/callback",
                credentials_path=root / "credentials.json",
                state_path=root / "state.json",
                state_ttl_minutes=15,
                api_version="5.199",
            )
            url = create_authorization_url(settings, now=datetime(2026, 8, 11, tzinfo=timezone.utc))
            state = parse_qs(urlparse(url).query)["state"][0]
            with patch("brucebet.vk_oauth.exchange_authorization_code", return_value={"access_token": "stored-token", "user_id": 7}):
                complete_authorization(settings, code="short-lived-code", state=state)

            self.assertEqual(load_server_access_token(settings.credentials_path), "stored-token")
            self.assertFalse(settings.state_path.exists())

    def test_rejects_non_https_callback(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VK_OAUTH_CLIENT_ID": "54715552",
                "VK_OAUTH_CLIENT_SECRET": "secret",
                "VK_OAUTH_REDIRECT_URI": "http://example.test/vk/oauth/callback",
            },
            clear=True,
        ):
            with self.assertRaises(VkOAuthConfigurationError):
                VkOAuthSettings.from_env()

    def test_worker_relay_exchanges_pending_code_without_returning_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = VkOAuthSettings(
                client_id="54715552",
                client_secret="server-secret",
                redirect_uri="https://brucebet-vk-oauth.example.workers.dev/vk/oauth/callback",
                credentials_path=root / "credentials.json",
                state_path=root / "state.json",
                state_ttl_minutes=15,
                api_version="5.199",
                worker_url="https://brucebet-vk-oauth.example.workers.dev",
                worker_relay_secret="relay-secret-that-is-long-enough",
            )
            create_authorization_url(settings)
            captured = {}

            def opener(request, **_kwargs):
                captured["url"] = request.full_url
                captured["authorization"] = request.get_header("Authorization")
                return FakeBytesResponse(b'{"code":"short-lived-code"}')

            with patch("brucebet.vk_oauth.exchange_authorization_code", return_value={"access_token": "stored-token", "user_id": 7}):
                self.assertTrue(complete_worker_relay_authorization(settings, opener=opener))

            self.assertEqual(captured["authorization"], "Bearer relay-secret-that-is-long-enough")
            self.assertIn("/vk/oauth/pending?state=", captured["url"])
            self.assertEqual(load_server_access_token(settings.credentials_path), "stored-token")
            self.assertFalse(settings.state_path.exists())

    def test_worker_relay_keeps_waiting_when_no_code_has_arrived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = VkOAuthSettings(
                client_id="54715552",
                client_secret="server-secret",
                redirect_uri="https://brucebet-vk-oauth.example.workers.dev/vk/oauth/callback",
                credentials_path=root / "credentials.json",
                state_path=root / "state.json",
                state_ttl_minutes=15,
                api_version="5.199",
                worker_url="https://brucebet-vk-oauth.example.workers.dev",
                worker_relay_secret="relay-secret-that-is-long-enough",
            )
            create_authorization_url(settings)

            self.assertFalse(
                complete_worker_relay_authorization(settings, opener=lambda *_args, **_kwargs: FakeBytesResponse(b""))
            )
            self.assertTrue(settings.state_path.exists())

    def test_worker_configuration_requires_its_exact_callback_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VK_OAUTH_CLIENT_ID": "54715552",
                "VK_OAUTH_CLIENT_SECRET": "secret",
                "VK_OAUTH_WORKER_URL": "https://brucebet-vk-oauth.example.workers.dev",
                "VK_OAUTH_WORKER_RELAY_SECRET": "relay-secret-that-is-long-enough",
                "VK_OAUTH_REDIRECT_URI": "https://wrong.example.test/vk/oauth/callback",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(VkOAuthConfigurationError, "must be the Worker callback URL"):
                VkOAuthSettings.from_env()


if __name__ == "__main__":
    unittest.main()
