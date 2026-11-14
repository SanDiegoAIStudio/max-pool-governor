#!/usr/bin/env python3
"""Detector tests. Every test points state at a temporary directory.

source: a test must not touch the real state directory.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_GUARD_TMP = tempfile.TemporaryDirectory(prefix="governor-guard-")
os.environ["MAX_POOL_STATE_DIR"] = _GUARD_TMP.name

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import max_pool_governor as gov  # noqa: E402


def _real_state_dir() -> Path:
    return (Path.home() / ".max-pool-governor").resolve()


def assert_throwaway(path: Path) -> None:
    """source: refuse a flag path inside the real state directory."""
    got = path.resolve()
    real = _real_state_dir()
    if got == real or real in got.parents:
        raise RuntimeError("refusing to touch the real state directory")


_SAVED_FLAG = gov.MAX_LIMIT_FLAG


def setUpModule():
    global _SAVED_FLAG
    assert_throwaway(gov.MAX_LIMIT_FLAG)
    # The module may have been imported by another test module first, so the
    # import-time path sits in that module's guard directory, not this one.
    temp_root = Path(tempfile.gettempdir()).resolve()
    guarded = [p for p in gov.MAX_LIMIT_FLAG.resolve().parents
               if p.parent == temp_root and p.name.startswith("governor-guard-")]
    if not guarded:
        raise RuntimeError("test flag is outside its temporary directory")
    _SAVED_FLAG = gov.MAX_LIMIT_FLAG
    gov.MAX_LIMIT_FLAG = Path(_GUARD_TMP.name) / gov.FLAG_FILENAME
    assert_throwaway(gov.MAX_LIMIT_FLAG)
    root = Path(_GUARD_TMP.name).resolve()
    if root not in gov.MAX_LIMIT_FLAG.resolve().parents:
        raise RuntimeError("detector tests are not inside a temporary directory")


def tearDownModule():
    gov.MAX_LIMIT_FLAG = _SAVED_FLAG


# Shapes taken from notices the detector is meant to match. No account detail.
REAL_QUOTA_NOTICES = [
    "You've reached your plan limit. Switch to another model, or manage usage credits",
    "usage limit reached|1756500000",
    "You've hit your usage limit. Your limit will reset at 3am.",
    "usage limit reached. Your limit will reset at 3pm (America/Los_Angeles).",
    "5-hour limit reached / resets 3am",
    "API error (status 402 Payment Required): usage balance exhausted",
    "API error (status 429): rate limit",
    "quota exceeded for this project",
    "You have exceeded your quota",
]

ORDINARY_TEXT_WITH_402_OR_429 = [
    "TASK-0402 call the supplier back about the estimate",
    "TASK-0429 draft the 429 words intro",
    "The tracker has 402 open items tonight.",
    '[{"raw":"TASK-0955","ref":"TASK-0289","group":"billing","due":null}]',
]


class QuotaNoticesAreCaught(unittest.TestCase):
    """source: short limit notices must be recognised."""

    def test_every_real_notice_matches(self):
        """source: each kept notice shape matches the detector."""
        for notice in REAL_QUOTA_NOTICES:
            with self.subTest(notice=notice[:50]):
                self.assertTrue(gov.looks_like_quota(notice),
                                f"quota notice not recognised: {notice!r}")

    def test_camelcase_rate_limit_names_match(self):
        """source: real API error reasons write the words joined, with a capital R."""
        for notice in ("userRateLimitExceeded", "apiRateLimitExceeded"):
            with self.subTest(notice=notice):
                self.assertTrue(gov.looks_like_quota(stderr=notice), notice)

    def test_noun_first_usage_limit_reached_matches(self):
        """source: noun-first "usage limit reached" must match.

        A pattern that requires "your" between a verb and "limit" misses this
        wording. The notice has no "your" at all.
        """
        self.assertTrue(gov.looks_like_quota("usage limit reached"))

    def test_stderr_is_matched_regardless_of_length(self):
        """source: stderr is matched even when it is longer than the notice band."""
        long_stderr = ("traceback noise\n" * 60) + "usage limit reached"
        self.assertGreater(len(long_stderr), gov.QUOTA_NOTICE_MAX)
        self.assertTrue(gov.looks_like_quota("", stderr=long_stderr))


class OrdinaryTextIsNotAQuota(unittest.TestCase):
    """source: ordinary text that mentions a number or a limit is not a notice."""

    def test_ids_containing_402_or_429_are_not_quotas(self):
        """source: a bare 402 or 429 inside an id or a sentence is not a notice."""
        for text in ORDINARY_TEXT_WITH_402_OR_429:
            with self.subTest(text=text[:50]):
                self.assertFalse(gov.looks_like_quota(text),
                                 f"ordinary text misread as a quota: {text!r}")

    def test_words_ending_in_rate_before_limit_are_not_quotas(self):
        """source: a letter right before "rate" means a longer word, not a rate limit."""
        for text in ("accurate limits on the input size",
                     "generate limit checks for the batch",
                     "a moderate limit of 40 files"):
            with self.subTest(text=text):
                self.assertFalse(gov.looks_like_quota(stderr=text), text)
        for text in ("rate_limit_error", "x-ratelimit-remaining: 0"):
            with self.subTest(text=text):
                self.assertTrue(gov.looks_like_quota(stderr=text), text)

    def test_an_all_capital_word_ending_in_rate_is_not_a_rate_limit(self):
        """source: the camelCase branch must not accept a capital letter after a capital letter."""
        for text in ("SEPARATE LIMIT", "ACCURATE LIMITS"):
            with self.subTest(text=text):
                self.assertFalse(gov.looks_like_quota(stderr=text), text)

    def test_a_long_review_discussing_a_rate_limit_is_kept(self):
        """source: a long document that mentions a rate limit must not match."""
        review = ("**Weekly review**\n\nTASK-0289 the message rate limit "
                  "reached its window again, and TASK-0040 the old ticket is still open. ")
        review += "Further detail about the tracker. " * 20
        self.assertGreater(len(review), gov.QUOTA_NOTICE_MAX)
        self.assertFalse(gov.looks_like_quota(review))

    def test_long_reply_quoting_a_limit_title_is_not_a_quota(self):
        """source: a long reply that quotes a limit title must not match."""
        reply = ('[{"raw":"TASK-0289","ref":null,"title":"Top up the account, the console '
                 'shows 402 usage balance exhausted","group":"other"}, ')
        reply += '{"raw":"TASK-0300","ref":null,"title":"filler row","group":"other"}, ' * 6
        reply += ']'
        self.assertGreater(len(reply), gov.QUOTA_NOTICE_MAX)
        self.assertFalse(gov.looks_like_quota(reply),
                         "a long reply quoting a limit title would be logged as a quota")


class TheLengthGuardIsTheDiscriminator(unittest.TestCase):
    """source: the same words are a notice only when the text is short."""

    def test_same_text_short_is_a_quota_and_long_is_not(self):
        """source: one string, two verdicts, decided by length."""
        short = "usage limit reached"
        long_doc = short + (" and here is the rest of a long weekly review. " * 30)
        self.assertTrue(gov.looks_like_quota(short))
        self.assertFalse(gov.looks_like_quota(long_doc))

    def test_a_review_just_over_the_notice_band_is_kept(self):
        """source: a fixed 360 character review that mentions a rate limit is kept.

        The length is fixed, not derived from QUOTA_NOTICE_MAX, so widening the
        constant cannot move this goalpost with it.
        """
        review = "TASK-0289 the message rate limit reached its window again. "
        review += "Tracker detail follows. " * 14
        review = review[:360]
        self.assertEqual(len(review), 360)
        self.assertFalse(gov.looks_like_quota(review),
                         "a 360-character review must never read as a quota notice")

    def test_the_notice_band_sits_below_the_real_review_floor(self):
        """source: the notice band stays below 400 characters."""
        self.assertLess(gov.QUOTA_NOTICE_MAX, 400,
                        "QUOTA_NOTICE_MAX must stay below 400")

    def test_empty_input_is_never_a_quota(self):
        """source: empty input is not a notice."""
        self.assertFalse(gov.looks_like_quota())
        self.assertFalse(gov.looks_like_quota("", ""))
        self.assertFalse(gov.looks_like_quota("   \n  "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
