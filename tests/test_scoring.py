from datetime import datetime, timezone
import unittest

from brucebet.scoring import Score, is_standard_score, normalize_score, is_prediction_eligible, parse_score, score_prediction
from brucebet.analytics import prediction_is_eligible


class ScoringTest(unittest.TestCase):
    def test_exact_score_gets_three(self) -> None:
        award = score_prediction(Score(2, 1), Score(2, 1))
        self.assertEqual(award.points, 3)
        self.assertEqual(award.category, "exact")

    def test_same_difference_gets_two(self) -> None:
        award = score_prediction(Score(2, 1), Score(1, 0))
        self.assertEqual(award.points, 2)
        self.assertEqual(award.category, "diff")

    def test_same_outcome_gets_one(self) -> None:
        award = score_prediction(Score(3, 1), Score(1, 0))
        self.assertEqual(award.points, 1)
        self.assertEqual(award.category, "outcome")

    def test_common_nonstandard_formats_are_accepted(self) -> None:
        self.assertEqual(parse_score("2 : 0"), Score(2, 0))
        self.assertEqual(parse_score("2-0"), Score(2, 0))
        self.assertEqual(parse_score("2;0"), Score(2, 0))
        self.assertEqual(normalize_score("2-0"), "2:0")
        self.assertFalse(is_standard_score("2-0"))
        self.assertTrue(is_standard_score("2:0"))
        self.assertIsNone(parse_score("10:0"))

    def test_lock_time(self) -> None:
        kickoff = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)
        early = datetime(2026, 6, 22, 16, 30, tzinfo=timezone.utc)
        late = datetime(2026, 6, 22, 16, 31, tzinfo=timezone.utc)
        self.assertTrue(is_prediction_eligible(early, kickoff))
        self.assertFalse(is_prediction_eligible(late, kickoff))

    def test_round_deadline_falls_back_to_match_cutoff(self) -> None:
        deadline = datetime(2026, 6, 18, 17, 30, tzinfo=timezone.utc)
        submitted = datetime(2026, 6, 18, 18, 44, tzinfo=timezone.utc)
        soon = datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc)
        later = datetime(2026, 6, 18, 21, 0, tzinfo=timezone.utc)

        self.assertFalse(prediction_is_eligible(submitted, None, deadline))
        self.assertFalse(prediction_is_eligible(submitted, soon, deadline))
        self.assertTrue(prediction_is_eligible(submitted, later, deadline))


if __name__ == "__main__":
    unittest.main()
