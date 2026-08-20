from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

try:
    from brucebet.telegram_app import forecast_cmd, load_settings, text_handler

    TELEGRAM_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "telegram":
        raise
    TELEGRAM_AVAILABLE = False

from brucebet.storage import connect, reset_db, upsert_match


class FakeMessage:
    def __init__(self, text: str, date: datetime, message_id: int = 100) -> None:
        self.text = text
        self.date = date
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def fake_update(text: str, date: str, message_id: int = 100, chat_id: int = 42):
    message = FakeMessage(text, datetime.fromisoformat(date), message_id)
    return SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=chat_id))


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot is installed in Docker and CI")
class TelegramForecastHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "forecasters.sqlite"
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE12345",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42",
            "BRUCEBET_DB_PATH": str(self.db_path),
            "BRUCEBET_DATA_DIR": self.tmp.name,
            "BRUCEBET_AUTO_SYNC": "0",
            "VK_TOPIC_DISCOVERY_ENABLED": "0",
            "VK_REGISTRATION_SYNC_ENABLED": "0",
            "VK_PREDICTIONS_SNAPSHOT_ENABLED": "0",
            "VK_OAUTH_ENABLED": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            self.settings = load_settings()
        conn = connect(self.db_path)
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2030-08-21T20:00:00+01:00", None)
        conn.commit()
        conn.close()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings}),
            args=[],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_forecast_command_uses_settings_and_records_aware_timestamp(self) -> None:
        update = fake_update("/forecast Igor | 1\n2:1", "2030-08-21T17:00:00+01:00")

        await forecast_cmd(update, self.context)

        conn = connect(self.db_path)
        prediction = conn.execute("SELECT score, submitted_at FROM predictions").fetchone()
        revision = conn.execute(
            "SELECT stable_source_item_id, eligibility_decision FROM prediction_revisions"
        ).fetchone()
        conn.close()
        self.assertEqual((prediction["score"], prediction["submitted_at"]), ("2:1", "2030-08-21T17:00:00+01:00"))
        self.assertEqual(revision["stable_source_item_id"], "telegram:42:100:position-1")
        self.assertEqual(revision["eligibility_decision"], "accepted")
        self.assertTrue(update.effective_message.replies)

    async def test_plain_text_handler_imports_forecast(self) -> None:
        update = fake_update("Anna\n1:0", "2030-08-21T17:15:00+01:00", message_id=101)

        await text_handler(update, self.context)

        conn = connect(self.db_path)
        row = conn.execute(
            """
            SELECT p.name, pr.score
            FROM predictions pr JOIN participants p ON p.id = pr.participant_id
            """
        ).fetchone()
        conn.close()
        self.assertEqual((row["name"], row["score"]), ("Anna", "1:0"))

    async def test_invalid_forecast_is_quarantined_without_projection(self) -> None:
        update = fake_update("/forecast Igor | 1\n10:0", "2030-08-21T17:00:00+01:00", message_id=102)

        await forecast_cmd(update, self.context)

        conn = connect(self.db_path)
        revision = conn.execute(
            "SELECT parse_status, eligibility_decision, reason FROM prediction_revisions"
        ).fetchone()
        projection_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        self.assertEqual(projection_count, 0)
        self.assertEqual(
            (revision["parse_status"], revision["eligibility_decision"], revision["reason"]),
            ("invalid", "quarantined", "invalid_score"),
        )

    async def test_post_deadline_edit_is_a_rejected_revision(self) -> None:
        early = fake_update("/forecast Igor | 1\n2:1", "2030-08-21T17:00:00+01:00", message_id=103)
        late_edit = fake_update("/forecast Igor | 1\n3:0", "2030-08-21T18:31:00+01:00", message_id=103)

        await forecast_cmd(early, self.context)
        await forecast_cmd(late_edit, self.context)

        conn = connect(self.db_path)
        score = conn.execute("SELECT score FROM predictions").fetchone()[0]
        decisions = [tuple(row) for row in conn.execute(
            "SELECT eligibility_decision, reason FROM prediction_revisions ORDER BY id"
        )]
        conn.close()
        self.assertEqual(score, "2:1")
        self.assertEqual(decisions, [("accepted", "before_round_deadline"), ("rejected", "late_edit")])


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot is installed in Docker and CI")
class TelegramAccessSettingsTests(unittest.TestCase):
    def test_empty_whitelist_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE12345"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_ALLOWED_CHAT_IDS"):
                load_settings()

    def test_development_override_is_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE12345",
                "BRUCEBET_ALLOW_UNRESTRICTED_CHATS": "1",
            },
            clear=True,
        ):
            settings = load_settings()
        self.assertTrue(settings.allow_unrestricted_chats)
        self.assertFalse(settings.allowed_chat_ids)


if __name__ == "__main__":
    unittest.main()
