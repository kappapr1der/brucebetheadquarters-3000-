from __future__ import annotations

import json
import unittest

from brucebet.vk_board import VkApiError, VkBoardClient, parse_topic_url


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class VkBoardTests(unittest.TestCase):
    def test_parse_vk_ru_topic_url(self) -> None:
        self.assertEqual(
            parse_topic_url("https://vk.ru/topic-217130885_66960850"),
            (217130885, 66960850),
        )

    def test_parse_vk_com_topic_url_with_query(self) -> None:
        self.assertEqual(
            parse_topic_url("https://vk.com/topic-217130885_51728798?offset=20"),
            (217130885, 51728798),
        )

    def test_probe_topic_normalizes_authors(self) -> None:
        payload = {
            "response": {
                "count": 2,
                "items": [
                    {"id": 11, "from_id": 101, "date": 1700000000, "text": "Arsenal - Chelsea 2:1"},
                    {"id": 12, "from_id": -217130885, "date": 1700000060, "text": "Template"},
                ],
                "profiles": [{"id": 101, "first_name": "Ivan", "last_name": "Petrov"}],
                "groups": [{"id": 217130885, "name": "Forecasters Club"}],
            }
        }

        def opener(request, timeout=0):
            self.assertIn("board.getComments", request.full_url)
            self.assertIn("extended=1", request.full_url)
            return FakeResponse(payload)

        result = VkBoardClient("token", opener=opener).probe_topic(217130885, 66960850, limit=20)
        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.author_count, 2)
        self.assertEqual(result.comments[0].author_name, "Ivan Petrov")
        self.assertEqual(result.comments[1].author_name, "Forecasters Club")

    def test_vk_error_is_exposed_without_token_leak(self) -> None:
        payload = {"error": {"error_code": 5, "error_msg": "User authorization failed"}}

        def opener(request, timeout=0):
            return FakeResponse(payload)

        with self.assertRaisesRegex(VkApiError, r"VK API error 5"):
            VkBoardClient("super-secret", opener=opener).probe_topic(1, 2)


if __name__ == "__main__":
    unittest.main()
