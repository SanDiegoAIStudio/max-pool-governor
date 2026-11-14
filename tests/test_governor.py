#!/usr/bin/env python3
"""Flag protocol and command line tests. Every test uses a temporary directory.

source: a test must not touch the real state directory.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
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


def setUpModule():
    assert_throwaway(gov.MAX_LIMIT_FLAG)


class GovernorTestCase(unittest.TestCase):
    """source: redirect the flag file into a throwaway directory for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="governor-")
        self._real_flag = gov.MAX_LIMIT_FLAG
        self._old_state = os.environ.get("MAX_POOL_STATE_DIR")
        gov.MAX_LIMIT_FLAG = Path(self._tmp.name) / gov.FLAG_FILENAME
        os.environ["MAX_POOL_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._restore)
        assert_throwaway(gov.MAX_LIMIT_FLAG)
        root = Path(self._tmp.name).resolve()
        if root not in gov.MAX_LIMIT_FLAG.resolve().parents:
            raise RuntimeError("test flag is outside its temporary directory")

    def _restore(self):
        gov.MAX_LIMIT_FLAG = self._real_flag
        if self._old_state is None:
            os.environ.pop("MAX_POOL_STATE_DIR", None)
        else:
            os.environ["MAX_POOL_STATE_DIR"] = self._old_state
        self._tmp.cleanup()

    def write_raw(self, text: str):
        gov.MAX_LIMIT_FLAG.write_text(text, encoding="utf-8")

    def read_raw(self) -> str:
        return gov.MAX_LIMIT_FLAG.read_text(encoding="utf-8")

    def stamp(self, hours_ago: float) -> str:
        return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


class WritesDoNotErodeEachOther(GovernorTestCase):
    """source: a second writer must not erase the first writer's line."""

    def test_a_second_writer_does_not_erase_the_first(self):
        """source: two flag writes both remain readable."""
        gov.flag_quota("writer-a", "plan-east")
        gov.flag_quota("writer-b", "plan-west")
        self.assertLessEqual({"plan-east", "plan-west"}, gov.quota_flagged())

    def test_the_same_source_and_model_is_recorded_once(self):
        """source: repeating one source and model does not grow the file."""
        for _ in range(5):
            gov.flag_quota("writer-a", "plan-east")
        # One model line plus one pool line. Five calls, not five pairs.
        self.assertEqual(len(self.read_raw().strip().splitlines()), 2)

    def test_one_call_can_record_several_models(self):
        """source: one call can record more than one model."""
        gov.flag_quota("writer-a", ["plan-east", "plan-mid", "plan-west"])
        self.assertLessEqual({"plan-east", "plan-mid", "plan-west"},
                             gov.quota_flagged())

    def test_yesterdays_entries_are_dropped_on_write(self):
        """source: a write drops a line older than the pool window."""
        self.write_raw(f"{self.stamp(30)} writer-a (plan-east)\n")
        gov.flag_quota("writer-b", "plan-west")
        self.assertNotIn("plan-east", gov.quota_flagged(48))
        self.assertIn("plan-west", gov.quota_flagged(48))

    def test_an_entry_from_last_night_survives_the_date_rolling(self):
        """source: retention is by age, so a line from the previous calendar date stays."""
        fixed = datetime(2026, 1, 15, 0, 30).astimezone()
        with mock.patch.object(gov, "_now", return_value=fixed):
            self.write_raw("2026-01-14T23:50:00 writer-a (plan-east)\n")
            gov.flag_quota("writer-b", "plan-west")
            self.assertIn("plan-east", gov.quota_flagged(now=fixed),
                          "an entry inside the reader window was erased by the date rolling")

    def test_an_unparseable_line_survives_a_write(self):
        """source: a line with no parseable age is kept across a write."""
        self.write_raw("hand-kept 2026-08-29T03:00:00\n")
        gov.flag_quota("writer-a", "plan-east")
        self.assertIn("hand-kept", self.read_raw())

    def test_flag_quota_never_raises_on_an_unwritable_path(self):
        """source: an unwritable flag path does not raise."""
        gov.MAX_LIMIT_FLAG = Path("/nonexistent-root-dir-xyz/state/limit.flag")
        gov.flag_quota("writer-a", "plan-east")  # must not raise

    def test_empty_or_blank_models_write_nothing(self):
        """source: an empty model list does not create the flag file."""
        gov.flag_quota("writer-a", [])
        gov.flag_quota("writer-a", [""])
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())


class ConcurrentWritersAllSurvive(GovernorTestCase):
    """source: the lock stops one writer from dropping another's line."""

    def _worker(self) -> Path:
        worker = Path(self._tmp.name) / "worker.py"
        repo = Path(__file__).resolve().parents[1]
        worker.write_text(textwrap.dedent(f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(repo)!r})
            import max_pool_governor as gov
            gov.MAX_LIMIT_FLAG = Path({str(gov.MAX_LIMIT_FLAG)!r})
            gov.flag_quota(sys.argv[1], sys.argv[2])
        """))
        return worker

    def test_a_writer_blocks_while_the_lock_is_held(self):
        """source: a writer in another process waits while the lock is held."""
        worker = self._worker()
        gov.MAX_LIMIT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        with open(gov.MAX_LIMIT_FLAG.with_name(gov.MAX_LIMIT_FLAG.name + ".lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, str(worker), "job", "model-blocked"])
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.wait(timeout=3)
            finally:
                proc.kill()
                proc.wait(timeout=10)

    def test_six_concurrent_processes_all_land(self):
        """source: six processes writing at once all leave a line."""
        worker = self._worker()
        procs = [subprocess.Popen([sys.executable, str(worker), f"job{i}", f"model-{i}"])
                 for i in range(6)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        self.assertLessEqual({f"model-{i}" for i in range(6)}, gov.quota_flagged(),
                             "a concurrent writer's entry was lost")


class ClearingIsScoped(GovernorTestCase):
    """source: clearing one model leaves the others in place."""

    def test_clearing_one_model_leaves_the_others(self):
        """source: clear removes only the named model."""
        gov.flag_quota("writer-a", "plan-east", scope="model")
        gov.flag_quota("writer-b", "plan-west", scope="model")
        gov.clear_quota(["plan-west"])
        self.assertEqual(gov.quota_flagged(), {"plan-east"})

    def test_the_file_is_removed_once_nothing_is_left(self):
        """source: the flag file is removed when the last line is cleared."""
        gov.flag_quota("writer-a", "plan-east")
        gov.clear_quota(["plan-east"])
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_an_unparseable_line_is_kept_not_silently_dropped(self):
        """source: clear keeps a line it cannot parse."""
        self.write_raw("hand-kept note about the pool\n")
        gov.clear_quota(["plan-east"])
        self.assertIn("hand-kept", self.read_raw())

    def test_clear_all_removes_the_file(self):
        """source: clear with no model list removes the file."""
        gov.flag_quota("writer-a", "plan-east")
        gov.clear_quota()
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_clear_quota_never_raises_when_the_file_is_absent(self):
        """source: clearing a missing file does not raise."""
        gov.clear_quota(["plan-east"])


class FreshnessIsPerLineNotFileMtime(GovernorTestCase):
    """source: each line expires from its own timestamp, not the file mtime."""

    def test_a_stale_line_in_a_freshly_touched_file_is_stale(self):
        """source: touching the file does not make an old line fresh."""
        self.write_raw(f"{self.stamp(30)} writer-a (plan-east)\n")
        os.utime(gov.MAX_LIMIT_FLAG, None)  # mtime = now, as a re-merge would leave it
        self.assertEqual(gov.quota_flagged(24), set(),
                         "an mtime-based read would call this fresh")

    def test_each_window_sees_only_what_falls_inside_it(self):
        """source: the cooldown, tonight, and pool windows each see their own lines."""
        self.write_raw(f"{self.stamp(2)} a (model-recent)\n"
                       f"{self.stamp(8)} b (model-mid)\n"
                       f"{self.stamp(20)} c (model-old)\n")
        self.assertEqual(gov.quota_flagged(gov.QUOTA_WINDOW_COOLDOWN_H), {"model-recent"})
        self.assertEqual(gov.quota_flagged(gov.QUOTA_WINDOW_TONIGHT_H),
                         {"model-recent", "model-mid"})
        self.assertEqual(gov.quota_flagged(gov.QUOTA_WINDOW_POOL_H),
                         {"model-recent", "model-mid", "model-old"})

    def test_the_windows_stay_ordered(self):
        """source: cooldown is shorter than tonight, and tonight is shorter than the pool window."""
        self.assertLess(gov.QUOTA_WINDOW_COOLDOWN_H, gov.QUOTA_WINDOW_TONIGHT_H)
        self.assertLess(gov.QUOTA_WINDOW_TONIGHT_H, gov.QUOTA_WINDOW_POOL_H)

    def test_a_missing_file_is_all_clear(self):
        """source: a missing flag file is not flagged."""
        self.assertEqual(gov.quota_flagged(), set())
        self.assertFalse(gov.quota_flag_fresh())

    def test_unparseable_content_still_reads_as_degraded(self):
        """source: unparseable flag text still counts as fresh."""
        self.write_raw("hand-kept 2026-08-29T03:00:00\n")
        self.assertEqual(gov.quota_flagged(), set())
        self.assertTrue(gov.quota_flag_fresh())

    def test_an_empty_file_is_not_degraded(self):
        """source: a blank flag file is not fresh."""
        self.write_raw("\n  \n")
        self.assertFalse(gov.quota_flag_fresh())


class ModelMatchingIsExact(GovernorTestCase):
    """source: a model name matches the whole field, not a prefix or the source field."""

    def test_a_prefix_of_another_model_does_not_match(self):
        """source: a shorter name that is a prefix of a flagged model is not flagged."""
        gov.flag_quota("writer-a", "plan-east-5", scope="model")
        self.assertNotIn("plan-east", gov.quota_flagged())
        self.assertEqual(gov.quota_flagged(), {"plan-east-5"})

    def test_the_source_field_is_never_returned_as_a_model(self):
        """source: the source name is not returned as a flagged model."""
        gov.flag_quota("writer-a", "plan-east")
        self.assertNotIn("writer-a", gov.quota_flagged())


class CreditAndTokenWordings(unittest.TestCase):
    """source: credit and token wordings match, and ordinary prose still does not."""

    def test_credit_and_token_wordings_still_match(self):
        """source: out of credits, out of tokens, and credits exhausted match."""
        for notice in ("You are out of credits for this billing period",
                       "out of tokens", "credits exhausted"):
            with self.subTest(notice=notice):
                self.assertTrue(gov.looks_like_quota(notice))

    def test_credit_and_token_branches_leave_prose_alone(self):
        """source: those branches do not match ordinary prose."""
        for text in ("the queue ran out of patience, not credits",
                     "The tracker has 402 open items tonight."):
            with self.subTest(text=text[:40]):
                self.assertFalse(gov.looks_like_quota(text))


class AccountLevelExhaustionTakesTheWholePool(GovernorTestCase):
    """source: an account-level notice blocks other models on the same pool."""

    def test_an_account_notice_blocks_a_sibling_model(self):
        """source: a sibling model on the same pool is flagged."""
        gov.flag_quota("writer-a", "plan-east", scope="account")
        self.assertTrue(gov.is_quota_flagged("plan-west"))
        self.assertTrue(gov.is_quota_flagged("plan-east"))

    def test_an_account_notice_does_not_block_another_vendor(self):
        """source: a model on a different pool stays available."""
        gov.flag_quota("writer-a", "plan-east", scope="account")
        self.assertFalse(gov.is_quota_flagged("other-4"))
        self.assertFalse(gov.is_quota_flagged("local-model:latest"))

    def test_a_model_scoped_notice_leaves_siblings_alone(self):
        """source: a model-scoped notice does not block a sibling."""
        gov.flag_quota("writer-a", "plan-east", scope="model")
        self.assertTrue(gov.is_quota_flagged("plan-east"))
        self.assertFalse(gov.is_quota_flagged("plan-west"))

    def test_the_common_wordings_classify_as_account(self):
        """source: notices that do not name another model are account scope."""
        for notice in ("usage limit reached",
                       "5-hour limit reached / resets 3am",
                       "You've hit your usage limit. Your limit will reset at 3am."):
            with self.subTest(notice=notice[:40]):
                self.assertEqual(gov.quota_scope(notice), "account")

    def test_a_switch_models_notice_classifies_as_model(self):
        """source: a notice that says to switch models is model scope."""
        self.assertEqual(gov.quota_scope(
            "You've reached your plan limit. Switch to another model, or manage usage "
            "credits"), "model")


class RepeatHitsRefreshTheRecord(GovernorTestCase):
    """source: a repeat hit moves the timestamp forward."""

    def test_a_repeat_hit_advances_the_timestamp(self):
        """source: an old line for the same source and model becomes fresh again."""
        self.write_raw(f"{self.stamp(7)} writer-a (plan-east)\n")
        self.assertFalse(gov.is_quota_flagged("plan-east",
                                              gov.QUOTA_WINDOW_COOLDOWN_H))
        gov.flag_quota("writer-a", "plan-east")
        self.assertTrue(gov.is_quota_flagged("plan-east",
                                             gov.QUOTA_WINDOW_COOLDOWN_H),
                        "a pool proven empty seconds ago reads as available")


class ClocksAndBytesCannotWedgeTheGovernor(GovernorTestCase):
    """source: a future stamp, a bad byte, or a naive stamp cannot wedge the reader."""

    def test_a_future_stamp_is_ignored(self):
        """source: a stamp past the clock skew allowance is ignored."""
        self.write_raw(f"{self.stamp(-12)} writer-a (plan-east)\n")
        self.assertEqual(gov.quota_flagged(), set())

    def test_a_stamp_inside_the_skew_allowance_is_honoured(self):
        """source: a stamp a few seconds ahead is still read."""
        self.write_raw(f"{self.stamp(-0.01)} writer-a (plan-east)\n")
        self.assertIn("plan-east", gov.quota_flagged())

    def test_one_bad_byte_does_not_discard_the_valid_entries(self):
        """source: one invalid byte does not hide a valid line."""
        good = f"{self.stamp(1)} writer-a (plan-east)\n".encode()
        gov.MAX_LIMIT_FLAG.write_bytes(b"\xff\xfe garbage line\n" + good)
        self.assertIn("plan-east", gov.quota_flagged())

    def test_an_empty_model_field_is_not_a_model(self):
        """source: an empty model field is not flagged."""
        self.write_raw(f"{self.stamp(1)} writer-a ()\n")
        self.assertEqual(gov.quota_flagged(), set())

    def test_naive_stamps_from_before_the_aware_switch_still_parse(self):
        """source: a naive timestamp still parses as local time."""
        naive = (datetime.now() - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        self.write_raw(f"{naive} writer-a (plan-east)\n")
        self.assertIn("plan-east", gov.quota_flagged())

    def test_written_stamps_carry_a_timezone(self):
        """source: a stamp written by flag_quota carries a timezone."""
        gov.flag_quota("writer-a", "plan-east")
        stamp = self.read_raw().split()[0]
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)


class ClearingCannotEraseANewerObservation(GovernorTestCase):
    """source: clear keeps a line newer than the evidence it was given."""

    def test_an_entry_newer_than_the_evidence_survives(self):
        """source: an entry written after observed_at is kept."""
        observed = datetime.now().astimezone() - timedelta(minutes=10)
        gov.flag_quota("writer-a", "plan-east", scope="model")
        gov.clear_quota(["plan-east"], observed_at=observed)
        self.assertIn("plan-east", gov.quota_flagged(),
                      "a newer exhaustion record was erased by older evidence of health")

    def test_an_entry_older_than_the_evidence_is_cleared(self):
        """source: an entry older than observed_at is cleared."""
        self.write_raw(f"{self.stamp(3)} writer-a (plan-east)\n")
        gov.clear_quota(["plan-east"],
                        observed_at=datetime.now().astimezone() - timedelta(minutes=10))
        self.assertNotIn("plan-east", gov.quota_flagged())

    def test_clearing_a_model_also_clears_its_pool_marker(self):
        """source: clearing a model also clears the pool marker written with it."""
        gov.flag_quota("writer-a", "plan-east", scope="account")
        gov.clear_quota(["plan-east"])
        self.assertFalse(gov.is_quota_flagged("plan-west"))


class TheCliIsTheInterfaceForNonPythonCallers(GovernorTestCase):
    """source: the command line exit codes are the contract."""

    MODULE = Path(__file__).resolve().parents[1] / "max_pool_governor.py"

    def _cli(self, *argv, stdin=""):
        env = {**os.environ, "MAX_POOL_STATE_DIR": str(self._tmp.name)}
        return subprocess.run(
            [sys.executable, str(self.MODULE), *argv],
            input=stdin, capture_output=True, text=True, timeout=60, env=env)

    def test_detect_flags_a_real_notice_and_exits_zero(self):
        """source: detect exits 0 and records the model when stdin is a notice."""
        proc = self._cli("detect", "scheduled-job", "plan-east",
                         stdin="usage limit reached")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("plan-east", gov.quota_flagged())

    def test_detect_exits_nonzero_on_an_ordinary_log(self):
        """source: detect exits 1 and writes nothing for an ordinary log."""
        proc = self._cli("detect", "scheduled-job", "plan-east",
                         stdin="processed 40 files\nwrote the report\ndone")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_detect_finds_a_notice_buried_in_a_long_log(self):
        """source: detect matches a notice inside a long log."""
        proc = self._cli("detect", "scheduled-job", "plan-east",
                         stdin=("noise line\n" * 200) + "usage limit reached\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_pool_only_record_blocks_the_whole_pool(self):
        """source: flagging a pool name blocks models on that pool."""
        self._cli("flag", "scheduled-job", "pool:plan")
        self.assertTrue(gov.is_quota_flagged("plan-east"))
        self.assertNotIn("pool", gov.quota_flagged() - {"pool:plan"})

    def test_check_exit_codes_are_the_contract(self):
        """source: check exits 0 when the model is flagged and 1 when it is not."""
        self._cli("flag", "shell-job", "plan-east", "--scope", "model")
        self.assertEqual(self._cli("check", "plan-east").returncode, 0)
        self.assertEqual(self._cli("check", "other-4").returncode, 1)

    def test_status_is_parseable_json_with_honest_ages(self):
        """source: status prints JSON ages from the line stamp, not the file mtime."""
        self.write_raw(f"{self.stamp(3)} writer-a (plan-east)\n")
        os.utime(gov.MAX_LIMIT_FLAG, None)  # mtime = now, as a re-merge leaves it
        payload = json.loads(self._cli("status", "--window", "24").stdout)
        self.assertEqual(payload["flagged"], ["plan-east"])
        self.assertEqual(payload["sources"], ["writer-a"])
        self.assertAlmostEqual(payload["newest_age_h"], 3, delta=0.2)

    def test_an_unknown_subcommand_fails_loudly(self):
        """source: an unknown subcommand exits 2."""
        proc = self._cli("frobnicate")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown subcommand", proc.stderr)

    def test_a_bad_window_fails_loudly_rather_than_defaulting(self):
        """source: a non-numeric window exits 2."""
        self.assertEqual(self._cli("check", "x", "--window", "soon").returncode, 2)


def _iso_in(hours: float) -> str:
    """source: an aware ISO time `hours` from now; negative is in the past."""
    return (datetime.now().astimezone() + timedelta(hours=hours)).isoformat()


class AnUntilDecidesHowLongALineIsLive(GovernorTestCase):
    """source: a line with its own expiry ignores the window in both directions."""

    def test_a_line_with_a_future_until_is_flagged_although_its_stamp_is_older_than_the_window(self):
        """source: stamp 30 hours old, until 2 hours ahead, window 24 hours: flagged."""
        self.write_raw(f"{self.stamp(30)} writer-a (plan-east) until={_iso_in(2)}\n")
        self.assertEqual(gov.quota_flagged(24), {"plan-east"})
        self.assertTrue(gov.is_quota_flagged("plan-east", 24))

    def test_a_line_whose_until_has_passed_is_not_flagged_although_its_stamp_is_inside_the_window(self):
        """source: stamp 1 hour old, until 1 hour ago, window 24 hours: not flagged."""
        self.write_raw(f"{self.stamp(1)} writer-a (plan-east) until={_iso_in(-1)}\n")
        self.assertEqual(gov.quota_flagged(24), set())
        self.assertFalse(gov.is_quota_flagged("plan-east", 24))

    def test_a_line_with_an_unparseable_until_is_read_as_a_line_without_one(self):
        """source: a bad until neither drops the line nor extends it."""
        self.write_raw(f"{self.stamp(1)} writer-a (plan-east) until=soonish\n"
                       f"{self.stamp(30)} writer-b (plan-west) until=soonish\n")
        self.assertEqual(gov.quota_flagged(24), {"plan-east"})
        self.assertEqual(gov.quota_untils(24), {})

    def test_a_file_in_the_old_format_with_no_until_still_reads_as_before(self):
        """source: lines with no until are live while their stamp is inside the window."""
        self.write_raw(f"{self.stamp(3)} writer-a (plan-east)\n")
        self.assertEqual(gov.quota_flagged(24), {"plan-east"})
        self.assertEqual(gov.quota_flagged(2), set())
        self.assertEqual(gov.quota_untils(), {})

    def test_the_line_stops_at_its_until_inside_a_widened_window(self):
        """source: a passed until ends the line however wide the window is."""
        self.write_raw(f"{self.stamp(1)} writer-a (plan-east) until={_iso_in(-0.5)}\n")
        self.assertEqual(gov.quota_flagged(1000), set())

    def test_a_future_stamp_with_an_until_is_still_ignored(self):
        """source: a stamp beyond the clock skew allowance is ignored, until or not."""
        self.write_raw(f"{self.stamp(-12)} writer-a (plan-east) until={_iso_in(5)}\n")
        self.assertEqual(gov.quota_flagged(), set())

    def test_quota_entries_keeps_its_tuple_shape(self):
        """source: quota_entries still returns (when, source, name) sorted."""
        self.write_raw(f"{self.stamp(1)} writer-b (plan-west) until={_iso_in(2)}\n"
                       f"{self.stamp(3)} writer-a (plan-east)\n")
        got = gov.quota_entries(24)
        self.assertEqual([(src, name) for _, src, name in got],
                         [("writer-a", "plan-east"), ("writer-b", "plan-west")])

    def test_a_pool_line_with_an_until_blocks_a_sibling_model(self):
        """source: is_quota_flagged reads the pool line and its until."""
        self.write_raw(f"{self.stamp(30)} writer-a (pool:plan) until={_iso_in(2)}\n")
        self.assertTrue(gov.is_quota_flagged("plan-west", 24))
        self.assertFalse(gov.is_quota_flagged("", 24))

    def test_quota_untils_gives_the_latest_live_until_for_each_name(self):
        """source: the latest of the live untils wins; a passed one and a line without one do not count."""
        late, early = _iso_in(5), _iso_in(2)
        self.write_raw(f"{self.stamp(1)} writer-a (plan-east) until={early}\n"
                       f"{self.stamp(1)} writer-b (plan-east) until={late}\n"
                       f"{self.stamp(1)} writer-a (plan-west) until={_iso_in(-1)}\n"
                       f"{self.stamp(1)} writer-a (plan-mid)\n")
        got = gov.quota_untils(24)
        self.assertEqual(list(got), ["plan-east"])
        self.assertEqual(got["plan-east"], datetime.fromisoformat(late))

    def test_clear_removes_a_line_that_carries_an_until(self):
        """source: clear_quota is unchanged and still removes the named model."""
        gov.flag_quota("writer-a", "plan-east", until=datetime.now() + timedelta(hours=2))
        gov.clear_quota(["plan-east"])
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())


class FlagQuotaWritesTheUntil(GovernorTestCase):
    """source: every line a call writes ends with the same until."""

    def test_the_name_line_and_the_pool_line_both_carry_the_until(self):
        """source: an account scope write puts the until on both lines."""
        until = datetime.now().astimezone() + timedelta(hours=3)
        gov.flag_quota("writer-a", "plan-east", until=until)
        lines = self.read_raw().strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.endswith(f" until={until.isoformat()}"), line)

    def test_a_naive_until_is_read_as_local_time(self):
        """source: a naive until is written as an aware local time."""
        naive = datetime.now() + timedelta(hours=3)
        gov.flag_quota("writer-a", "plan-east", scope="model", until=naive)
        written = self.read_raw().split("until=")[1].strip()
        self.assertEqual(datetime.fromisoformat(written), naive.astimezone())

    def test_no_until_writes_the_old_line_format(self):
        """source: without an until the line is STAMP SOURCE (NAME) and nothing more."""
        gov.flag_quota("writer-a", "plan-east", scope="model")
        self.assertNotIn("until", self.read_raw())
        self.assertEqual(len(self.read_raw().split()), 3)

    def test_a_repeat_hit_replaces_the_until(self):
        """source: the second call's until replaces the first call's until."""
        first = datetime.now().astimezone() + timedelta(hours=1)
        second = datetime.now().astimezone() + timedelta(hours=5)
        gov.flag_quota("writer-a", "plan-east", until=first)
        gov.flag_quota("writer-a", "plan-east", until=second)
        self.assertEqual(len(self.read_raw().strip().splitlines()), 2)
        self.assertEqual(gov.quota_untils()["plan-east"], second)
        self.assertNotIn(first.isoformat(), self.read_raw())

    def test_a_write_keeps_another_sources_line_older_than_a_day_when_its_until_is_ahead(self):
        """source: a line stamped 30 hours ago with an until 2 hours ahead survives a write."""
        self.write_raw(f"{self.stamp(30)} writer-a (plan-east) until={_iso_in(2)}\n")
        gov.flag_quota("writer-b", "plan-west")
        self.assertIn("(plan-east)", self.read_raw())
        self.assertIn("plan-east", gov.quota_flagged())

    def test_a_write_drops_another_sources_line_whose_until_has_passed(self):
        """source: a line stamped 2 hours ago whose until passed is dropped by a write."""
        self.write_raw(f"{self.stamp(2)} writer-a (plan-east) until={_iso_in(-1)}\n")
        gov.flag_quota("writer-b", "plan-west")
        self.assertNotIn("plan-east", self.read_raw())

    def test_a_line_with_an_unparseable_until_survives_a_write_inside_the_window(self):
        """source: a bad until is read as no until, so the line follows the window."""
        self.write_raw(f"{self.stamp(2)} writer-a (plan-east) until=soonish\n")
        gov.flag_quota("writer-b", "plan-west")
        self.assertIn("plan-east", self.read_raw())


class TheCliCarriesTheUntil(GovernorTestCase):
    """source: detect, flag and status carry the reset time on the command line."""

    MODULE = Path(__file__).resolve().parents[1] / "max_pool_governor.py"

    def _cli(self, *argv, stdin=""):
        env = {**os.environ, "MAX_POOL_STATE_DIR": str(self._tmp.name)}
        return subprocess.run(
            [sys.executable, str(self.MODULE), *argv],
            input=stdin, capture_output=True, text=True, timeout=60, env=env)

    def _untils_in_file(self):
        return [line.split("until=")[1] for line in self.read_raw().strip().splitlines()
                if "until=" in line]

    def test_detect_with_a_clock_time_writes_the_same_until_on_the_name_line_and_the_pool_line(self):
        """source: "reset at 3am" gives one until, written on both lines and printed."""
        proc = self._cli("detect", "scheduled-job", "plan-east",
                         stdin="You've hit your usage limit. Your limit will reset at 3am.")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        untils = self._untils_in_file()
        self.assertEqual(len(untils), 2)
        self.assertEqual(untils[0], untils[1])
        when = datetime.fromisoformat(untils[0])
        now = datetime.now().astimezone()
        self.assertGreater(when, now)
        self.assertLessEqual(when, now + timedelta(hours=25))
        self.assertEqual(proc.stdout.strip(), f"flagged plan-east (account) until {untils[0]}")

    def test_detect_with_an_epoch_prints_and_writes_that_moment(self):
        """source: "limit reached|EPOCH" gives exactly that moment."""
        epoch = int(datetime.now().timestamp()) + 3600
        proc = self._cli("detect", "scheduled-job", "plan-east", "--scope", "model",
                         stdin=f"usage limit reached|{epoch}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = datetime.fromisoformat(self._untils_in_file()[0])
        self.assertEqual(got.timestamp(), epoch)
        self.assertTrue(proc.stdout.startswith("flagged plan-east (model) until "))

    def test_detect_with_no_readable_time_prints_the_short_form_and_writes_no_until(self):
        """source: a notice with no time is flagged for the window."""
        proc = self._cli("detect", "scheduled-job", "plan-east", stdin="usage limit reached")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "flagged plan-east (account)\n")
        self.assertNotIn("until", self.read_raw())

    def test_flag_until_writes_it_among_the_other_arguments(self):
        """source: --until may sit among the arguments, like --window."""
        until = (datetime.now() + timedelta(hours=40)).replace(microsecond=0)
        proc = self._cli("flag", "--until", until.isoformat(), "shell-job", "plan-east",
                         "--scope", "model", "--window", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(datetime.fromisoformat(self._untils_in_file()[0]),
                         until.astimezone())

    def test_flag_until_does_not_apply_the_eight_day_guard(self):
        """source: a time the owner typed may be more than 8 days ahead."""
        until = (datetime.now() + timedelta(days=30)).replace(microsecond=0)
        proc = self._cli("flag", "shell-job", "plan-east", "--until", until.isoformat())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._cli("check", "plan-east").returncode, 0)

    def test_flag_until_in_the_past_exits_two_and_writes_nothing(self):
        """source: --until must be in the future."""
        past = (datetime.now() - timedelta(hours=1)).replace(microsecond=0).isoformat()
        proc = self._cli("flag", "shell-job", "plan-east", "--until", past)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--until must be in the future", proc.stderr)
        self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_flag_until_that_does_not_parse_exits_two_and_writes_nothing(self):
        """source: a missing or unparseable --until value exits 2."""
        for argv in (("flag", "shell-job", "plan-east", "--until", "tomorrow"),
                     ("flag", "shell-job", "plan-east", "--until")):
            with self.subTest(argv=argv):
                proc = self._cli(*argv)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("--until wants an ISO time such as 2026-01-31T03:00",
                              proc.stderr)
                self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_status_lists_the_until_of_a_flagged_name(self):
        """source: status has an until object from each flagged name to its ISO time."""
        until = (datetime.now() + timedelta(hours=4)).replace(microsecond=0)
        self._cli("flag", "shell-job", "plan-east", "--until", until.isoformat())
        payload = json.loads(self._cli("status").stdout)
        self.assertEqual(sorted(payload["until"]), ["plan-east", "pool:plan"])
        for text in payload["until"].values():
            self.assertEqual(datetime.fromisoformat(text), until.astimezone())
        self.assertEqual(list(payload)[-1], "until")

    def test_status_until_is_empty_when_no_name_carries_one(self):
        """source: status prints {} for until when nothing has a reset time."""
        self._cli("flag", "shell-job", "plan-east", "--scope", "model")
        payload = json.loads(self._cli("status").stdout)
        self.assertEqual(payload["flagged"], ["plan-east"])
        self.assertEqual(payload["until"], {})
        gov.clear_quota()
        self.assertEqual(json.loads(self._cli("status").stdout)["until"], {})


class TheCliRulesTheReadmeStates(GovernorTestCase):
    """source: each command line rule the README states has a test that fails without it."""

    MODULE = Path(__file__).resolve().parents[1] / "max_pool_governor.py"
    NOTICE = "usage limit reached"

    def _cli(self, *argv, stdin=""):
        env = {**os.environ, "MAX_POOL_STATE_DIR": str(self._tmp.name)}
        return subprocess.run(
            [sys.executable, str(self.MODULE), *argv],
            input=stdin, capture_output=True, text=True, timeout=60, env=env)

    def _flag_aged(self, hours_ago: float, name: str = "plan-east", touch: bool = False):
        self.write_raw(f"{self.stamp(hours_ago)} writer-a ({name})\n")
        if touch:
            then = time.time() - hours_ago * 3600
            os.utime(gov.MAX_LIMIT_FLAG, (then, then))

    def test_no_arguments_and_dash_h_exit_zero_and_name_the_subcommands(self):
        """source: no argument, -h and --help each exit 0 and name the six subcommands."""
        for argv in ((), ("-h",), ("--help",)):
            with self.subTest(argv=argv):
                proc = self._cli(*argv)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                for name in ("detect", "flag", "check", "fresh", "status", "clear"):
                    self.assertIn(name, proc.stdout)

    def test_a_bad_scope_exits_two_and_writes_nothing(self):
        """source: --scope takes model or account only, and a bad one writes nothing."""
        cases = (
            (("flag", "nightly", "plan-east", "--scope", "everything"), ""),
            (("flag", "nightly", "plan-east", "--scope"), ""),
            (("detect", "nightly", "plan-east", "--scope", "everything"), self.NOTICE),
        )
        for argv, stdin in cases:
            with self.subTest(argv=argv):
                proc = self._cli(*argv, stdin=stdin)
                self.assertEqual(proc.returncode, 2)
                if argv[0] == "flag":
                    self.assertIn("--scope wants model or account", proc.stderr)
                self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_check_window_replaces_the_default_window(self):
        """source: check --window HOURS replaces the 24 hour default for that call."""
        self._flag_aged(30)
        self.assertEqual(self._cli("check", "plan-east").returncode, 1)
        self.assertEqual(self._cli("check", "plan-east", "--window", "48").returncode, 0)
        self._flag_aged(2)
        self.assertEqual(self._cli("check", "plan-east").returncode, 0)
        self.assertEqual(self._cli("check", "plan-east", "--window", "1").returncode, 1)

    def test_fresh_window_replaces_the_default_window(self):
        """source: fresh --window HOURS replaces the default, and the file time follows it."""
        self._flag_aged(30, touch=True)
        self.assertEqual(self._cli("fresh").returncode, 1)
        self.assertEqual(self._cli("fresh", "--window", "48").returncode, 0)
        self._flag_aged(2, touch=True)
        self.assertEqual(self._cli("fresh").returncode, 0)
        self.assertEqual(self._cli("fresh", "--window", "1").returncode, 1)

    def test_status_window_replaces_the_default_window(self):
        """source: status --window HOURS replaces the default for that call."""
        self._flag_aged(30)
        self.assertEqual(json.loads(self._cli("status").stdout)["flagged"], [])
        wide = json.loads(self._cli("status", "--window", "48").stdout)
        self.assertEqual(wide["flagged"], ["plan-east"])

    def test_status_ages_are_newest_and_oldest(self):
        """source: newest_age_h is the youngest line and oldest_age_h is the oldest."""
        self.write_raw(f"{self.stamp(1)} writer-a (plan-east)\n"
                       f"{self.stamp(5)} writer-a (plan-west)\n")
        payload = json.loads(self._cli("status").stdout)
        self.assertGreaterEqual(payload["newest_age_h"], 0.9)
        self.assertLessEqual(payload["newest_age_h"], 1.2)
        self.assertGreaterEqual(payload["oldest_age_h"], 4.9)
        self.assertLessEqual(payload["oldest_age_h"], 5.2)

    def test_flag_without_a_model_exits_two_and_writes_nothing(self):
        """source: flag wants a source and at least one model."""
        for argv in (("flag", "nightly"), ("flag",)):
            with self.subTest(argv=argv):
                proc = self._cli(*argv)
                self.assertEqual(proc.returncode, 2)
                self.assertTrue(proc.stderr.strip())
                self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_detect_without_a_model_exits_two_and_writes_nothing(self):
        """source: detect wants a source and a model before it reads the notice."""
        for argv in (("detect", "nightly"), ("detect",)):
            with self.subTest(argv=argv):
                proc = self._cli(*argv, stdin=self.NOTICE)
                self.assertEqual(proc.returncode, 2)
                self.assertFalse(gov.MAX_LIMIT_FLAG.exists())

    def test_clear_without_a_model_exits_two_and_clears_nothing(self):
        """source: clear with no model exits 2 and leaves the flag in place."""
        self._flag_aged(1)
        self.assertEqual(self._cli("clear").returncode, 2)
        self.assertEqual(self._cli("check", "plan-east").returncode, 0)

    def test_check_without_a_model_exits_two(self):
        """source: check with no model is a usage error, not an answer."""
        proc = self._cli("check")
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip())

    def test_an_account_notice_for_a_pool_name_writes_one_line(self):
        """source: a name that starts with pool: gets no second pool line."""
        gov.flag_quota("writer-a", "pool:default", scope="account")
        lines = self.read_raw().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("(pool:default)"), lines[0])
        self.assertEqual(gov.quota_flagged(), {"pool:default"})

    def test_pool_of_takes_the_text_before_the_first_dash_or_colon(self):
        """source: pool_of cuts at the first dash or colon, lower cases, and groups the claude names."""
        self.assertEqual(gov.pool_of("plan-east"), "plan")
        self.assertEqual(gov.pool_of("plan-west"), "plan")
        for name in ("fable", "opus", "sonnet", "haiku"):
            with self.subTest(name=name):
                self.assertEqual(gov.pool_of(f"{name}-5"), "claude")
                self.assertEqual(gov.pool_of(name.upper()), "claude")
        self.assertEqual(gov.pool_of("pool:default"), "pool")
        self.assertEqual(gov.pool_of(""), "unknown")
        gov.flag_quota("writer-a", "opus-5", scope="account")
        self.assertTrue(gov.is_quota_flagged("sonnet-5"))
        self.assertFalse(gov.is_quota_flagged("plan-east"))

    def test_flag_until_reads_a_trailing_z(self):
        """source: a trailing Z on --until is UTC, on every Python."""
        moment = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
        text = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._cli("flag", "nightly", "plan-east", "--until", text).returncode, 0)
        payload = json.loads(self._cli("status").stdout)
        self.assertEqual(datetime.fromisoformat(payload["until"]["plan-east"]), moment)


if __name__ == "__main__":
    unittest.main(verbosity=2)
