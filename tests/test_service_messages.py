from datetime import datetime, timezone
import unittest

from brucebet.service_messages import (
    DEFAULT_REMINDER_OFFSETS_MINUTES,
    deadline_after_message,
    deadline_reminder_for_offset,
    deadline_reminder_schedule,
    deadline_schedule_created,
    participants_loaded,
    render,
    round_template_loaded,
    user_forecast_loaded,
)


class ServiceMessagesTest(unittest.TestCase):
    def test_user_forecast_flow_points_to_participant_forecasts(self) -> None:
        text = render(
            user_forecast_loaded(
                participant="Bruce Wayne",
                round_name="3",
                rows_count=24,
                expected_count=24,
            )
        )
        self.assertIn("Принято, Bruce Wayne", text)
        self.assertIn("Теперь кидай прогнозы участников", text)

    def test_user_forecast_reports_issues(self) -> None:
        text = render(
            user_forecast_loaded(
                participant="Bruce Wayne",
                round_name="2",
                rows_count=23,
                expected_count=24,
                missing_count=1,
                invalid_count=2,
                nonstandard_count=3,
                late_needs_check_count=3,
            )
        )
        self.assertIn("Не хватает прогнозов: 1", text)
        self.assertIn("Нестандартных форматов счёта: 3", text)
        self.assertIn("Я их принял и нормализовал", text)
        self.assertIn("Нечитаемых счетов: 2", text)
        self.assertIn("После дедлайна: 3", text)

    def test_template_message_has_deadline(self) -> None:
        deadline = datetime(2026, 6, 22, 20, 30, tzinfo=timezone.utc)
        text = render(round_template_loaded("3", 24, deadline))
        self.assertIn("Тур 3", text)
        self.assertIn("2026-06-22T20:30:00+00:00", text)

    def test_participants_message_has_bank(self) -> None:
        text = render(participants_loaded(16, 12))
        self.assertIn("Текущий банк: 3600 руб", text)

    def test_deadline_schedule_uses_default_offsets(self) -> None:
        deadline = datetime(2026, 6, 22, 20, 30, tzinfo=timezone.utc)
        plan = deadline_reminder_schedule(deadline)

        self.assertEqual([item.offset_minutes for item in plan], list(DEFAULT_REMINDER_OFFSETS_MINUTES))
        self.assertEqual(plan[0].send_at, datetime(2026, 6, 21, 20, 30, tzinfo=timezone.utc))
        self.assertEqual(plan[-1].send_at, datetime(2026, 6, 22, 20, 10, tzinfo=timezone.utc))

    def test_deadline_schedule_created_message_lists_offsets(self) -> None:
        deadline = datetime(2026, 6, 22, 20, 30, tzinfo=timezone.utc)
        text = render(deadline_schedule_created("3", deadline))

        self.assertIn("Напоминания на тур 3 поставлены", text)
        self.assertIn("24 часа, 6 часов, 3 часа, 1 час, 20 минут", text)

    def test_deadline_twenty_minute_message_is_urgent(self) -> None:
        deadline = datetime(2026, 6, 22, 20, 30, tzinfo=timezone.utc)
        text = deadline_reminder_for_offset(deadline, 20).text

        self.assertIn("До дедлайна 20 минут", text)
        self.assertIn("Последний нормальный шанс", text)

    def test_deadline_after_message_mentions_partial_late_rule(self) -> None:
        deadline = datetime(2026, 6, 22, 20, 30, tzinfo=timezone.utc)
        text = deadline_after_message(deadline).text

        self.assertIn("Дедлайн прошёл", text)
        self.assertIn("kickoff - 90 минут", text)


if __name__ == "__main__":
    unittest.main()
