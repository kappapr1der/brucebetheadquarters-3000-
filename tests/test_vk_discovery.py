from __future__ import annotations

import unittest

from brucebet.storage import (
    connect,
    init_db,
    mark_vk_topic_alert_sent,
    record_vk_topic_discovery,
)
from brucebet.vk_board import VkDiscoveredTopic


def topic(topic_id: int, *, title: str = "Прогнозы на АПЛ") -> VkDiscoveredTopic:
    return VkDiscoveredTopic(
        group_id=217130885,
        topic_id=topic_id,
        url=f"https://vk.ru/topic-217130885_{topic_id}",
        title=title,
        topic_kind="predictions",
        league_hint="epl",
    )


class VkDiscoveryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_first_discovery_pass_baselines_existing_topics(self) -> None:
        baseline, alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [topic(100)],
            checked_at="2026-08-09T10:00:00+00:00",
        )
        self.assertTrue(baseline)
        self.assertEqual(alerts, [])

        baseline, alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [topic(100), topic(101)],
            checked_at="2026-08-09T10:30:00+00:00",
        )
        self.assertFalse(baseline)
        self.assertEqual([item.topic_id for item in alerts], [101])

        mark_vk_topic_alert_sent(self.conn, alerts[0], sent_at="2026-08-09T10:31:00+00:00")
        _, retry_alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [topic(100), topic(101)],
            checked_at="2026-08-09T11:00:00+00:00",
        )
        self.assertEqual(retry_alerts, [])

    def test_non_epl_topics_never_enter_notification_queue(self) -> None:
        rpl = VkDiscoveredTopic(
            group_id=217130885,
            topic_id=102,
            url="https://vk.ru/topic-217130885_102",
            title="Прогнозы РПЛ",
            topic_kind="predictions",
            league_hint="non_epl",
        )
        baseline, alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [],
            checked_at="2026-08-09T10:00:00+00:00",
        )
        self.assertFalse(baseline)
        self.assertEqual(alerts, [])

        baseline, alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [rpl],
            checked_at="2026-08-09T10:10:00+00:00",
        )
        self.assertTrue(baseline)
        self.assertEqual(alerts, [])
        _, alerts = record_vk_topic_discovery(
            self.conn,
            217130885,
            [rpl],
            checked_at="2026-08-09T10:30:00+00:00",
        )
        self.assertEqual(alerts, [])
        count = self.conn.execute("SELECT COUNT(*) AS count FROM vk_topic_alerts").fetchone()["count"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
