from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

try:
    from brucebet.telegram_app import (
        deliver_pending_vk_prediction_notifications,
        forecast_cmd,
        load_settings,
        notify_vk_access_challenge,
        text_handler,
        vk_predictions_snapshot_job,
    )

    TELEGRAM_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "telegram":
        raise
    TELEGRAM_AVAILABLE = False

from brucebet.storage import connect, ensure_participant, reset_db, upsert_match
from brucebet.vk_prediction_notifications import enqueue_vk_prediction_notification


class FakeMessage:
    def __init__(self, text: str, date: datetime, message_id: int = 100) -> None:
        self.text = text
        self.date = date
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self, failing_chat_ids: set[int] | None = None) -> None:
        self.failing_chat_ids = failing_chat_ids or set()
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        if chat_id in self.failing_chat_ids:
            raise RuntimeError(f"chat {chat_id} is unavailable")
        self.messages.append((chat_id, text))


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
        # Forecast ingestion must not create contestants implicitly. These are
        # the explicit roster entries used by the handler scenarios below.
        ensure_participant(conn, "Igor", paid=1)
        ensure_participant(conn, "Anna", paid=1)
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

    async def test_vk_access_challenge_stays_out_of_telegram(self) -> None:
        bot = FakeBot()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings}),
            bot=bot,
        )

        await notify_vk_access_challenge(context, self.settings)

        self.assertEqual(bot.messages, [])

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

    async def test_vk_prediction_job_writes_only_behind_explicit_import_gate(self) -> None:
        report = SimpleNamespace(topic_id=67251746, forecast_submissions=(object(),))
        archive = SimpleNamespace(created=False)
        imported = SimpleNamespace(
            topic_id=67251746,
            submissions_seen=1,
            forecasts_seen=10,
            revisions_created=10,
            duplicates=0,
            accepted=10,
            rejected=0,
            quarantined=0,
            issues=(),
        )
        disabled_context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings}),
        )
        enabled_context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"settings": replace(self.settings, vk_predictions_import_enabled=True)}
            ),
        )
        with (
            patch("brucebet.telegram_app.read_vk_predictions_worker", return_value=(report, archive)),
            patch("brucebet.telegram_app.record_vk_predictions_worker", return_value=imported) as record,
            patch("brucebet.telegram_app.deliver_pending_vk_prediction_notifications") as deliver,
        ):
            await vk_predictions_snapshot_job(disabled_context)
            record.assert_not_called()
            deliver.assert_not_called()
            await vk_predictions_snapshot_job(enabled_context)
            record.assert_called_once_with(enabled_context.application.bot_data["settings"], report)
            deliver.assert_awaited_once_with(enabled_context, enabled_context.application.bot_data["settings"])

    async def test_vk_prediction_outbox_retries_per_allowed_chat_without_duplicate_delivery(self) -> None:
        conn = connect(self.db_path)
        event_created = enqueue_vk_prediction_notification(
            conn,
            kind="new",
            group_id=217130885,
            topic_id=67251746,
            source_key="vk:sergey:2030-08-10T12:10:00+03:00",
            content_fingerprint="fixture-content",
            participant_name="Сергей",
            vk_author="Mr Sam",
            round_name="1",
            payload={"accepted": 10, "expected": 10, "deadline_at": "2030-08-21T18:30:00+01:00"},
            chat_ids=(42, 77),
            created_at="2030-08-10T13:00:00+03:00",
        )
        conn.commit()
        conn.close()
        self.assertTrue(event_created)
        settings = replace(self.settings, allowed_chat_ids=frozenset({42, 77}), vk_predictions_import_enabled=True)
        failing_bot = FakeBot({42})
        context = SimpleNamespace(bot=failing_bot)

        await deliver_pending_vk_prediction_notifications(context, settings)

        self.assertEqual([chat_id for chat_id, _text in failing_bot.messages], [77])
        conn = connect(self.db_path)
        first_attempt = [
            tuple(row)
            for row in conn.execute(
                """
                SELECT chat_id, status, attempts, error IS NOT NULL
                FROM vk_prediction_notification_deliveries
                ORDER BY chat_id
                """
            )
        ]
        conn.close()
        self.assertEqual(first_attempt, [(42, "pending", 1, 1), (77, "sent", 0, 0)])

        retry_bot = FakeBot()
        retry_context = SimpleNamespace(bot=retry_bot)
        await deliver_pending_vk_prediction_notifications(retry_context, settings)
        await deliver_pending_vk_prediction_notifications(retry_context, settings)

        self.assertEqual([chat_id for chat_id, _text in retry_bot.messages], [42])
        conn = connect(self.db_path)
        final_attempt = [
            tuple(row)
            for row in conn.execute(
                "SELECT chat_id, status, attempts FROM vk_prediction_notification_deliveries ORDER BY chat_id"
            )
        ]
        conn.close()
        self.assertEqual(final_attempt, [(42, "sent", 1), (77, "sent", 0)])


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
