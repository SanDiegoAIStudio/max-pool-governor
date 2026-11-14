#!/usr/bin/env python3
"""Tests for reset_time. Every test passes `now` in and points state at a temporary directory.

source: the reset time in a notice decides how long a job waits.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_GUARD_TMP = tempfile.TemporaryDirectory(prefix="governor-guard-")
os.environ["MAX_POOL_STATE_DIR"] = _GUARD_TMP.name

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import max_pool_governor as gov  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


class ResetTimeCase(unittest.TestCase):
    """source: tests that need the local zone get America/Los_Angeles, then restore it."""

    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()


class TheEpochShape(ResetTimeCase):
    """source: a ten digit epoch after a pipe is a reset time in UTC."""

    def test_an_epoch_after_a_pipe_is_read(self):
        """source: "usage limit reached|1756500000" names the moment 1756500000 UTC."""
        now = datetime.fromtimestamp(1756500000 - 3600, tz=UTC)
        got = gov.reset_time("usage limit reached|1756500000", now)
        self.assertEqual(got, datetime.fromtimestamp(1756500000, tz=UTC))
        self.assertIsNotNone(got.tzinfo)

    def test_spaces_after_the_pipe_are_allowed(self):
        """source: the pipe may be followed by spaces before the digits."""
        now = datetime.fromtimestamp(1756500000 - 3600, tz=UTC)
        self.assertEqual(gov.reset_time("limit reached |  1756500000", now),
                         datetime.fromtimestamp(1756500000, tz=UTC))

    def test_eleven_digits_are_not_an_epoch(self):
        """source: exactly ten digits, not followed by another digit."""
        now = datetime.fromtimestamp(1756500000 - 3600, tz=UTC)
        self.assertIsNone(gov.reset_time("limit reached|17565000001", now))

    def test_an_epoch_in_the_past_gives_none(self):
        """source: a result not later than now is None."""
        now = datetime.fromtimestamp(1756500000 + 60, tz=UTC)
        self.assertIsNone(gov.reset_time("usage limit reached|1756500000", now))

    def test_an_epoch_more_than_eight_days_ahead_gives_none(self):
        """source: a result more than RESET_HORIZON_DAYS days ahead is None."""
        self.assertEqual(gov.RESET_HORIZON_DAYS, 8)
        epoch = 1756500000
        inside = datetime.fromtimestamp(epoch - 7 * 86400, tz=UTC)
        outside = datetime.fromtimestamp(epoch - 9 * 86400, tz=UTC)
        self.assertIsNotNone(gov.reset_time(f"limit reached|{epoch}", inside))
        self.assertIsNone(gov.reset_time(f"limit reached|{epoch}", outside))

    def test_the_epoch_shape_is_tried_before_the_clock_shape(self):
        """source: the first shape found decides; a clock time in the same text is ignored."""
        now = datetime.fromtimestamp(1756500000 - 3600, tz=UTC)
        got = gov.reset_time("limit reached|1756500000 resets 3am", now)
        self.assertEqual(got, datetime.fromtimestamp(1756500000, tz=UTC))

    def test_an_epoch_that_fails_the_guard_does_not_fall_through_to_the_clock(self):
        """source: when the shape that decides yields None the answer is None."""
        now = datetime.fromtimestamp(1756500000 + 60, tz=UTC)
        self.assertIsNone(gov.reset_time("limit reached|1756500000 resets 3am", now))


class TheClockShape(ResetTimeCase):
    """source: a clock time after the word reset names the next such time."""

    def test_before_three_am_gives_today_at_three_am(self):
        """source: "resets 3am" read at 01:00 gives 03:00 the same day."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        got = gov.reset_time("5-hour limit reached / resets 3am", now)
        self.assertEqual(got, datetime(2026, 5, 10, 3, 0, tzinfo=LA))

    def test_after_three_am_gives_tomorrow_at_three_am(self):
        """source: "resets 3am" read at 05:00 gives 03:00 the next day."""
        now = datetime(2026, 5, 10, 5, 0, tzinfo=LA)
        got = gov.reset_time("Your limit will reset at 3am.", now)
        self.assertEqual(got, datetime(2026, 5, 11, 3, 0, tzinfo=LA))

    def test_the_moment_itself_is_not_later_than_now(self):
        """source: the result is strictly after now, so exactly 3am gives tomorrow."""
        now = datetime(2026, 5, 10, 3, 0, tzinfo=LA)
        got = gov.reset_time("resets 3am", now)
        self.assertEqual(got, datetime(2026, 5, 11, 3, 0, tzinfo=LA))

    def test_minutes_are_read(self):
        """source: "resets 3:30pm" is 15:30."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        got = gov.reset_time("resets 3:30pm", now)
        self.assertEqual(got, datetime(2026, 5, 10, 15, 30, tzinfo=LA))

    def test_pm_is_afternoon_and_case_does_not_matter(self):
        """source: pm adds twelve hours, in either case."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertEqual(gov.reset_time("Resets 7PM", now).hour, 19)
        self.assertEqual(gov.reset_time("resets 7am", now).hour, 7)

    def test_twelve_am_is_midnight_and_twelve_pm_is_noon(self):
        """source: 12am is hour 0 and 12pm is hour 12."""
        now = datetime(2026, 5, 10, 5, 0, tzinfo=LA)
        midnight = gov.reset_time("resets 12am", now)
        noon = gov.reset_time("resets 12pm", now)
        self.assertEqual(midnight, datetime(2026, 5, 11, 0, 0, tzinfo=LA))
        self.assertEqual(noon, datetime(2026, 5, 10, 12, 0, tzinfo=LA))

    def test_an_hour_of_zero_or_thirteen_gives_none(self):
        """source: the hour must be 1 to 12."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("resets 0am", now))
        self.assertIsNone(gov.reset_time("resets 13pm", now))

    def test_minutes_above_fifty_nine_give_none(self):
        """source: the minutes must be 00 to 59."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("resets 3:75pm", now))

    def test_a_named_zone_gives_the_next_such_time_in_that_zone(self):
        """source: "reset at 3pm (Asia/Tokyo)" with now in UTC is 3pm in Tokyo."""
        now = datetime(2026, 5, 10, 0, 0, tzinfo=UTC)         # 09:00 in Tokyo
        got = gov.reset_time("reset at 3pm (Asia/Tokyo)", now)
        self.assertEqual(got, datetime(2026, 5, 10, 6, 0, tzinfo=UTC))
        self.assertEqual(got.astimezone(ZoneInfo("Asia/Tokyo")).hour, 15)
        later = datetime(2026, 5, 10, 8, 0, tzinfo=UTC)       # 17:00 in Tokyo
        self.assertEqual(gov.reset_time("reset at 3pm (Asia/Tokyo)", later),
                         datetime(2026, 5, 11, 6, 0, tzinfo=UTC))

    def test_no_zone_name_uses_the_local_zone(self):
        """source: without a zone name the machine's local zone is used."""
        now = datetime(2026, 5, 10, 7, 0, tzinfo=UTC)         # 00:00 in Los Angeles
        got = gov.reset_time("resets 3am", now)
        self.assertEqual(got, datetime(2026, 5, 10, 10, 0, tzinfo=UTC))

    def test_the_wall_clock_survives_the_spring_change_with_a_named_zone(self):
        """source: across 2026-03-08 in America/Los_Angeles the result still reads 3am."""
        now = datetime(2026, 3, 7, 13, 0, tzinfo=UTC)         # 05:00 PST on 03-07
        got = gov.reset_time("reset at 3am (America/Los_Angeles)", now)
        self.assertEqual(got, datetime(2026, 3, 8, 10, 0, tzinfo=UTC))   # 03:00 PDT
        self.assertEqual((got.astimezone(LA).hour, got.astimezone(LA).minute), (3, 0))

    def test_the_wall_clock_survives_the_spring_change_through_the_local_zone(self):
        """source: the same change through the local zone still reads 3am."""
        now = datetime(2026, 3, 7, 13, 0, tzinfo=UTC)
        got = gov.reset_time("resets 3am", now)
        self.assertEqual(got, datetime(2026, 3, 8, 10, 0, tzinfo=UTC))
        local = datetime.fromtimestamp(got.timestamp())
        self.assertEqual((local.hour, local.minute), (3, 0))

    def test_a_wall_time_the_zone_skips_is_not_a_moment(self):
        """source: 2:30am does not exist on 2026-03-08 in Los Angeles, so the next 2:30am is 03-09."""
        now = datetime(2026, 3, 7, 13, 0, tzinfo=UTC)
        got = gov.reset_time("reset at 2:30am (America/Los_Angeles)", now)
        self.assertEqual(got, datetime(2026, 3, 9, 9, 30, tzinfo=UTC))

    def test_the_wall_clock_survives_the_autumn_change(self):
        """source: across 2026-11-01 the result still reads 9am on the wall clock."""
        now = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)       # 13:00 PDT on 10-31
        got = gov.reset_time("reset at 9am (America/Los_Angeles)", now)
        self.assertEqual(got, datetime(2026, 11, 1, 17, 0, tzinfo=UTC))   # 09:00 PST

    def test_a_naive_now_is_read_as_local_time(self):
        """source: a naive now is local time."""
        got = gov.reset_time("resets 3am", datetime(2026, 5, 10, 1, 0))
        self.assertEqual(got, datetime(2026, 5, 10, 10, 0, tzinfo=UTC))

    def test_an_unknown_zone_gives_none(self):
        """source: a zone that does not load is None, never local time."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("reset at 3pm (Not/AZone)", now))
        self.assertIsNone(gov.reset_time("reset at 3pm ()", now))

    def test_a_notice_with_no_time_gives_none(self):
        """source: a notice that states no time is None."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("usage limit reached", now))

    def test_a_date_before_the_time_gives_none(self):
        """source: "resets Nov 20, 9am" does not follow the word reset directly."""
        now = datetime(2026, 11, 13, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("usage limit reached, resets Nov 20, 9am", now))


class OddInputNeverRaises(ResetTimeCase):
    """source: reset_time never raises."""

    def test_empty_and_very_long_text_give_none(self):
        """source: empty text, a very long text and a non-string give None."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        self.assertIsNone(gov.reset_time("", now))
        self.assertIsNone(gov.reset_time("x" * 2_000_000, now))
        self.assertIsNone(gov.reset_time("reset " * 200_000, now))
        self.assertIsNone(gov.reset_time("| " * 200_000, now))
        self.assertIsNone(gov.reset_time(None, now))
        self.assertIsNone(gov.reset_time(b"resets 3am", now))

    def test_a_very_long_text_with_a_notice_at_the_end_is_still_read(self):
        """source: the reader finds the shape inside a long log."""
        now = datetime(2026, 5, 10, 1, 0, tzinfo=LA)
        got = gov.reset_time(("noise\n" * 100_000) + "resets 3am", now)
        self.assertEqual(got, datetime(2026, 5, 10, 3, 0, tzinfo=LA))

    def test_the_default_now_is_the_module_clock(self):
        """source: with no now the module's _now is used."""
        with mock.patch.object(gov, "_now", return_value=datetime(2026, 5, 10, 1, 0, tzinfo=LA)):
            got = gov.reset_time("resets 3am")
        self.assertEqual(got, datetime(2026, 5, 10, 3, 0, tzinfo=LA))


if __name__ == "__main__":
    unittest.main(verbosity=2)
