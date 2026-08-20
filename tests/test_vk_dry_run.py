from __future__ import annotations

import unittest

from brucebet.vk_board import VkPublicTopicResult
from brucebet.vk_dry_run import parse_public_topic_result, render_dry_run_report


def topic_result(text: str, topic_id: int = 12345678) -> VkPublicTopicResult:
    return VkPublicTopicResult(
        group_id=217130885,
        topic_id=topic_id,
        url=f"https://vk.ru/topic-217130885_{topic_id}",
        html_chars=len(text),
        visible_chars=len(text),
        score_line_count=sum(1 for line in text.splitlines() if ":" in line),
        text=text,
    )


PREDICTIONS_TEXT = """
Forecasters Club
Прогнозы на АПЛ 2026/2027
Forecasters Club 9 авг 2026 в 10:00
Шаблон на АПЛ, 1-й тур. Дедлайн 21.08.2026, 20:30
Arsenal - Chelsea
Liverpool - Everton
Manchester City - Tottenham
Newcastle - Aston Villa
Brighton - Fulham
Brentford - Crystal Palace
Leeds - Sunderland
West Ham - Bournemouth
Nottingham Forest - Wolves
Burnley - Manchester United
Mr Sam
10 авг 2026 в 12:10
Сергей
Arsenal - Chelsea 2 : 1
Liverpool - Everton 1-0
Manchester City - Tottenham 3;1
Newcastle - Aston Villa 1:0
Brighton - Fulham 2:0
Brentford - Crystal Palace 1:1
Leeds - Sunderland 2:1
West Ham - Bournemouth 0:0
Nottingham Forest - Wolves 1:0
Burnley - Manchester United 0:2
Игорь Григорьев 10 авг 2026 в 12:12
Arsenal - Chelsea 1:1
Liverpool - Everton 2:1
"""


REGISTRATION_TEXT = """
Forecasters Club
Регистрация участников АПЛ 2026/2027
Forecasters Club 4 июн 2026 в 0:11
Для участия пишем имя и статус взноса.
Игорь Григорьев 4 июн 2026 в 0:13
Игорь Григорьев.
Взнос 300 рублей.
Mr Sam
4 июн 2026 в 11:40
Сергей
Без взноса
Регистрация закрыта. Финальный состав участников опубликован.
"""


LIVE_REGISTRATION_TEXT = """
Forecasters Club
Заявка на участие в прогнозах АПЛ 2026/2027
Forecasters Club
today at 6:51 pm
Для участия пишем имя и статус взноса.
Georgy Karev
today at 6:52 pm
Георгий Карев, без взноса
Mr Sam
today at 6:59 pm
Мр сэм (Сергей) , взн 500
Yury Efremychev
today at 7:21 pm
Ефремычев Юрий
500
"""


SHOW_LIKES_REGISTRATION_TEXT = """
Forecasters Club
Заявка на участие в прогнозах АПЛ 2026/2027
Sergey Kirillov
today at 6:59 pm
Show likes
Show more posts
Сергей Кириллов
Без взноса
Alexey Zakharov
today at 7:21 pm
Show likes
Алексей Захаров
Взнос 500 рублей
Andrzej Wisniewski
today at 8:00 pm
Взнос 500
Show likes
Show more posts
Загружается...
Go up
Read only the most interesting posts
We'll find posts according to your preferences and create a whole feed from them. Just sign in to check it out.
Sign up
Sign in
"""


ABSOLUTE_ENGLISH_DATE_REGISTRATION_TEXT = """
Forecasters Club
Заявка на участие в прогнозах АПЛ 2026/2027
Sergey Kirillov
14 Aug 2026 at 3:35 pm
Без взноса
Andrzej Wisniewski
20 Aug 2026 at 12:13 pm
Взнос 500
"""


class VkDryRunTests(unittest.TestCase):
    def test_predictions_recognize_split_author_actual_participant_and_partial_blocks(self) -> None:
        report = parse_public_topic_result(topic_result(PREDICTIONS_TEXT), "predictions")

        self.assertEqual(report.league_hint, "epl")
        self.assertEqual(len(report.templates), 1)
        self.assertEqual(len(report.forecast_submissions), 2)

        full, partial = report.forecast_submissions
        self.assertEqual(full.vk_author, "Mr Sam")
        self.assertEqual(full.participant, "Сергей")
        self.assertTrue(full.is_full)
        self.assertEqual([item.normalized_score for item in full.forecasts[:3]], ["2:1", "1:0", "3:1"])
        self.assertFalse(partial.is_full)
        self.assertEqual(len(partial.forecasts), 2)
        self.assertIn("missing fixture positions", " | ".join(partial.warnings))

    def test_registration_confirms_payment_and_marks_closure(self) -> None:
        report = parse_public_topic_result(topic_result(REGISTRATION_TEXT), "registration")

        self.assertEqual(report.registration_state, "closed")
        self.assertTrue(report.final_roster_detected)
        self.assertEqual(len(report.registration_entries), 2)
        paid, free = report.registration_entries
        self.assertEqual((paid.participant, paid.fee_intent, paid.payment_status), ("Игорь Григорьев", "paid_declared", "confirmed"))
        self.assertEqual((free.participant, free.fee_intent, free.payment_status), ("Сергей", "free", "not_applicable"))

    def test_registration_recognizes_live_relative_dates_names_and_fee_markers(self) -> None:
        report = parse_public_topic_result(topic_result(LIVE_REGISTRATION_TEXT), "registration")

        self.assertEqual(
            [(item.participant, item.fee_intent, item.fee_amount_rub) for item in report.registration_entries],
            [
                ("Георгий Карев", "free", None),
                ("Сергей", "paid_declared", 500),
                ("Ефремычев Юрий", "paid_declared", 500),
            ],
        )

    def test_registration_ignores_vk_show_likes_controls_before_resolving_participant(self) -> None:
        report = parse_public_topic_result(topic_result(SHOW_LIKES_REGISTRATION_TEXT), "registration")

        self.assertEqual(
            [(item.participant, item.fee_intent, item.fee_amount_rub) for item in report.registration_entries],
            [
                ("Сергей Кириллов", "free", None),
                ("Алексей Захаров", "paid_declared", 500),
                ("Andrzej Wisniewski", "paid_declared", 500),
            ],
        )
        self.assertNotIn("Show likes", [item.participant for item in report.registration_entries])
        self.assertNotIn("Show more posts", [item.participant for item in report.registration_entries])
        self.assertNotIn("Загружается", [item.participant for item in report.registration_entries])

    def test_registration_recognizes_absolute_english_vk_dates(self) -> None:
        report = parse_public_topic_result(topic_result(ABSOLUTE_ENGLISH_DATE_REGISTRATION_TEXT), "registration")

        self.assertEqual(
            [(item.vk_author, item.participant, item.fee_intent, item.fee_amount_rub) for item in report.registration_entries],
            [
                ("Sergey Kirillov", "Sergey Kirillov", "free", None),
                ("Andrzej Wisniewski", "Andrzej Wisniewski", "paid_declared", 500),
            ],
        )

    def test_comment_key_does_not_change_when_earlier_comments_are_added(self) -> None:
        complete = """
Forecasters Club
Регистрация АПЛ
First Person
10 Aug 2026 at 6:00 pm
Без взноса
Sergey Kirillov
14 Aug 2026 at 3:35 pm
Без взноса
"""
        later_only = """
Forecasters Club
Регистрация АПЛ
Sergey Kirillov
14 Aug 2026 at 3:35 pm
Без взноса
"""

        complete_report = parse_public_topic_result(topic_result(complete), "registration")
        later_report = parse_public_topic_result(topic_result(later_only), "registration")

        self.assertEqual(complete_report.registration_entries[-1].source_key, later_report.registration_entries[-1].source_key)

    def test_non_epl_topic_is_visible_but_never_future_ingestion_ready(self) -> None:
        report = parse_public_topic_result(topic_result("Прогнозы РПЛ\n"), "predictions")

        self.assertEqual(report.league_hint, "non_epl")
        self.assertFalse(report.future_ingestion_allowed)
        self.assertIn("ingestion must stay disabled", render_dry_run_report(report))


if __name__ == "__main__":
    unittest.main()

