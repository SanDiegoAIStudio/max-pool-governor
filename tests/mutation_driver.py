#!/usr/bin/env python3
"""source: each mutation must make its named test fail.

Copies the module and the tests into a temporary directory, checks the
selector passes, applies one mutation, and checks the selector fails.
Prints one line per mutation: killed or survived. Exits 1 if any survived.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, test module, old, new, selector[, file relative to the repository root])
# Without the sixth element the file is max_pool_governor.py.
MUTATIONS = [
    ("M1 narrow pattern misses noun-first wording",
     "test_detect",
     '    r"|\\b(?:status|http)\\s+(?:402|429)\\b",\n    re.I)',
     '    r"|\\b(?:status|http)\\s+(?:402|429)\\b",\n    re.I)\n'
     '# MUTANT: historical narrow pattern\n'
     'LIMIT_PAT = re.compile(r"hit your .{0,14}limit", re.I)',
     "QuotaNoticesAreCaught.test_noun_first_usage_limit_reached_matches"),

    ("M2 bare 402 or 429 alternation matches ordinary text",
     "test_detect",
     '    r"|\\b(?:status|http)\\s+(?:402|429)\\b",',
     '    r"|402|429",  # MUTANT: unanchored',
     "OrdinaryTextIsNotAQuota.test_ids_containing_402_or_429_are_not_quotas"),

    ("M3 length guard removed from model output",
     "test_detect",
     '    if body and len(body) <= QUOTA_NOTICE_MAX and LIMIT_PAT.search(body):',
     '    if body and LIMIT_PAT.search(body):  # MUTANT: no length guard',
     "OrdinaryTextIsNotAQuota.test_long_reply_quoting_a_limit_title_is_not_a_quota"),

    ("M4 noun-first branch dropped",
     "test_detect",
     '    r"|\\blimit\\s+reached\\b"',
     '    r"|MUTANT_NEVER_MATCHES_B"',
     "QuotaNoticesAreCaught.test_every_real_notice_matches"),

    ("M5 payment and balance branches dropped",
     "test_detect",
     '    r"|balance\\s+exhausted"\n'
     '    r"|payment\\s+required"\n'
     '    r"|\\b(?:status|http)\\s+(?:402|429)\\b",\n'
     '    re.I)',
     '    r"|MUTANT_NEVER_MATCHES_C",\n'
     '    re.I)',
     "QuotaNoticesAreCaught.test_every_real_notice_matches"),

    ("M6 stderr length-gated",
     "test_detect",
     '    if stderr and LIMIT_PAT.search(stderr):',
     '    if stderr and len(stderr) <= QUOTA_NOTICE_MAX and LIMIT_PAT.search(stderr):',
     "QuotaNoticesAreCaught.test_stderr_is_matched_regardless_of_length"),

    ("M7 notice band widened past 400",
     "test_detect",
     'QUOTA_NOTICE_MAX = 300',
     'QUOTA_NOTICE_MAX = 500  # MUTANT: overlaps a 360 character review',
     "TheLengthGuardIsTheDiscriminator.test_a_review_just_over_the_notice_band_is_kept"),

    ("G1 truncating write erases the other writer",
     "test_governor",
     '        tmp = MAX_LIMIT_FLAG.with_name(f"{MAX_LIMIT_FLAG.name}.tmp.{os.getpid()}")\n'
     '        tmp.write_text("\\n".join(merged) + "\\n", encoding="utf-8")\n'
     '        tmp.replace(MAX_LIMIT_FLAG)\n'
     '    except OSError:\n'
     '        if tmp is not None:\n'
     '            try:\n'
     '                tmp.unlink(missing_ok=True)\n'
     '            except OSError:\n'
     '                pass\n'
     '    finally:\n'
     '        if lock_fh is not None:\n'
     '            try:\n'
     '                fcntl.flock(lock_fh, fcntl.LOCK_UN)\n'
     '            finally:\n'
     '                lock_fh.close()\n'
     '\n'
     '\n'
     'def clear_quota',
     '        MAX_LIMIT_FLAG.write_text(  # MUTANT: truncating, erases other writers\n'
     '            f"{stamp} {source} ({models[-1]})\\n", encoding="utf-8")\n'
     '    except OSError:\n'
     '        if tmp is not None:\n'
     '            try:\n'
     '                tmp.unlink(missing_ok=True)\n'
     '            except OSError:\n'
     '                pass\n'
     '    finally:\n'
     '        if lock_fh is not None:\n'
     '            try:\n'
     '                fcntl.flock(lock_fh, fcntl.LOCK_UN)\n'
     '            finally:\n'
     '                lock_fh.close()\n'
     '\n'
     '\n'
     'def clear_quota',
     "WritesDoNotErodeEachOther.test_a_second_writer_does_not_erase_the_first"),

    ("G2 flock removed from the writer",
     "test_governor",
     '        lock_fh = open(MAX_LIMIT_FLAG.with_name(MAX_LIMIT_FLAG.name + ".lock"), "w")\n'
     '        fcntl.flock(lock_fh, fcntl.LOCK_EX)\n'
     '\n'
     '        now = _now()',
     '        lock_fh = None  # MUTANT: no lock\n'
     '\n'
     '        now = _now()',
     "ConcurrentWritersAllSurvive.test_a_writer_blocks_while_the_lock_is_held"),

    ("G3 stale entries retained on write",
     "test_governor",
     '            if _live(parsed, cutoff, now):\n'
     '                merged.append(line)\n',
     '            merged.append(line)  # MUTANT: never expires an entry\n',
     "WritesDoNotErodeEachOther.test_yesterdays_entries_are_dropped_on_write"),

    ("G3b retention uses the calendar date",
     "test_governor",
     '        cutoff = now - timedelta(hours=QUOTA_WINDOW_POOL_H)\n',
     '        cutoff = now.replace(hour=0, minute=0, second=0,\n'
     '                             microsecond=0)  # MUTANT: midnight cliff\n',
     "WritesDoNotErodeEachOther.test_an_entry_from_last_night_survives_the_date_rolling"),

    ("G4 freshness read from the file mtime",
     "test_governor",
     '        if parsed and _live(parsed, cutoff, now):\n            out.append(parsed[:3])',
     '        if parsed and MAX_LIMIT_FLAG.stat().st_mtime >= cutoff.timestamp():\n'
     '            out.append(parsed[:3])  # MUTANT: mtime, not the line stamp',
     "FreshnessIsPerLineNotFileMtime.test_a_stale_line_in_a_freshly_touched_file_is_stale"),

    ("G5 clear removes the whole file",
     "test_governor",
     '        if models is None:\n'
     '            MAX_LIMIT_FLAG.unlink(missing_ok=True)\n'
     '            return\n',
     '        MAX_LIMIT_FLAG.unlink(missing_ok=True)  # MUTANT: clears everything\n'
     '        return\n',
     "ClearingIsScoped.test_clearing_one_model_leaves_the_others"),

    ("G6 clear drops lines it cannot parse",
     "test_governor",
     '            if parsed is None:\n'
     '                kept.append(line)\n'
     '                continue\n',
     '            if parsed is None:  # MUTANT: drop what we cannot read\n'
     '                continue\n',
     "ClearingIsScoped.test_an_unparseable_line_is_kept_not_silently_dropped"),

    ("G7 credit and token branches dropped",
     "test_governor",
     '    r"|\\bout\\s+of\\s+(?:credits|tokens|quota)\\b"\n'
     '    r"|\\bcredits?\\s+(?:exhausted|depleted)\\b"\n',
     '    r"|MUTANT_NEVER_MATCHES_UNION"\n',
     "CreditAndTokenWordings.test_credit_and_token_wordings_still_match"),

    ("G11 substring model matching restored",
     "test_governor",
     '    return {model for _, _, model in quota_entries(max_age_h, now=now)}',
     '    try:  # MUTANT: raw-text scan\n'
     '        return {MAX_LIMIT_FLAG.read_text(encoding="utf-8")}\n'
     '    except OSError:\n'
     '        return set()',
     "ModelMatchingIsExact.test_a_prefix_of_another_model_does_not_match"),

    ("G14 detect length-guards the piped log",
     "test_governor",
     '        if not looks_like_quota(stderr=text):\n            return 1\n',
     '        if not looks_like_quota(text):  # MUTANT: body path, length-guarded\n'
     '            return 1\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_detect_finds_a_notice_buried_in_a_long_log"),

    ("G15 detect ignores stdin and always reports a quota",
     "test_governor",
     '        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")\n',
     '        text = "usage limit reached"  # MUTANT: ignores stdin\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_detect_exits_nonzero_on_an_ordinary_log"),

    ("G16 check inverts its exit code",
     "test_governor",
     '        return 0 if is_quota_flagged(rest[0], window) else 1\n',
     '        return 1 if is_quota_flagged(rest[0], window) else 0  # MUTANT: inverted\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_check_exit_codes_are_the_contract"),

    ("R1 the epoch shape is ignored",
     "test_reset_time",
     '        epoch = _EPOCH_SHAPE.search(text)\n',
     '        epoch = None  # MUTANT: epoch ignored\n',
     "TheEpochShape.test_an_epoch_after_a_pipe_is_read"),

    ("R2 pm is read as am",
     "test_reset_time",
     '    hour = hour % 12 + (12 if match.group(3).lower() == "pm" else 0)\n',
     '    hour = hour % 12  # MUTANT: pm read as am\n',
     "TheClockShape.test_pm_is_afternoon_and_case_does_not_matter"),

    ("R3 12am is read as noon",
     "test_reset_time",
     '    hour = hour % 12 + (12 if match.group(3).lower() == "pm" else 0)\n',
     '    hour = hour % 12 + (12 if match.group(3).lower() == "pm" or hour == 12 else 0)'
     '  # MUTANT: 12am is noon\n',
     "TheClockShape.test_twelve_am_is_midnight_and_twelve_pm_is_noon"),

    ("R4 the roll to tomorrow is removed",
     "test_reset_time",
     '    for days in range(3):\n',
     '    for days in range(1):  # MUTANT: today only\n',
     "TheClockShape.test_after_three_am_gives_tomorrow_at_three_am"),

    ("R5 a named zone is read as local time",
     "test_reset_time",
     '    tz = ZoneInfo(zone.strip()) if zone is not None else None\n',
     '    tz = None  # MUTANT: zone name ignored\n',
     "TheClockShape.test_a_named_zone_gives_the_next_such_time_in_that_zone"),

    ("R6 the horizon guard is removed",
     "test_reset_time",
     '        if found.timestamp() > now.timestamp() + RESET_HORIZON_DAYS * 86400:\n'
     '            return None\n',
     '        # MUTANT: no horizon guard\n',
     "TheEpochShape.test_an_epoch_more_than_eight_days_ahead_gives_none"),

    ("R7 the guard against a past time is removed",
     "test_reset_time",
     '        if found.timestamp() <= now.timestamp():\n'
     '            return None\n',
     '        # MUTANT: no past guard\n',
     "TheEpochShape.test_an_epoch_in_the_past_gives_none"),

    ("U1 a passed until still counts as live",
     "test_governor",
     '        return now < until\n',
     '        return True  # MUTANT: until never ends the line\n',
     "AnUntilDecidesHowLongALineIsLive."
     "test_a_line_whose_until_has_passed_is_not_flagged_although_its_stamp_is_inside_the_window"),

    ("U2 the window is applied to a line with a future until",
     "test_governor",
     '        return now < until\n',
     '        return now < until and when >= cutoff  # MUTANT: window applies\n',
     "AnUntilDecidesHowLongALineIsLive."
     "test_a_line_with_a_future_until_is_flagged_although_its_stamp_is_older_than_the_window"),

    ("U3 flag_quota drops the until when it writes",
     "test_governor",
     '        suffix = f" until={_aware(until).isoformat()}" if until is not None else ""\n',
     '        suffix = ""  # MUTANT: until dropped\n',
     "FlagQuotaWritesTheUntil.test_the_name_line_and_the_pool_line_both_carry_the_until"),

    ("U4 a write drops another source's old line whose until is ahead",
     "test_governor",
     '            if _live(parsed, cutoff, now):\n'
     '                merged.append(line)\n',
     '            if when >= cutoff:  # MUTANT: ignores the until\n'
     '                merged.append(line)\n',
     "FlagQuotaWritesTheUntil."
     "test_a_write_keeps_another_sources_line_older_than_a_day_when_its_until_is_ahead"),

    ("U5 an unparseable until drops its line",
     "test_governor",
     '        except ValueError:\n'
     '            until = None\n',
     '        except ValueError:\n'
     '            return None  # MUTANT: drops the line\n',
     "AnUntilDecidesHowLongALineIsLive."
     "test_a_line_with_an_unparseable_until_is_read_as_a_line_without_one"),

    ("C1 a past --until is accepted",
     "test_governor",
     '            if until <= _now():\n',
     '            if False:  # MUTANT: past time accepted\n',
     "TheCliCarriesTheUntil.test_flag_until_in_the_past_exits_two_and_writes_nothing"),

    ("C2 the status until key is always empty",
     "test_governor",
     '            "until": {name: when.isoformat() for name, when in sorted(untils.items())},\n',
     '            "until": {},  # MUTANT: always empty\n',
     "TheCliCarriesTheUntil.test_status_lists_the_until_of_a_flagged_name"),

    ("C3 detect drops the reset time it read",
     "test_governor",
     '        flag_quota(rest[0], rest[1], scope=used_scope, until=until)\n',
     '        flag_quota(rest[0], rest[1], scope=used_scope, until=None)  # MUTANT\n',
     "TheCliCarriesTheUntil."
     "test_detect_with_a_clock_time_writes_the_same_until_on_the_name_line_and_the_pool_line"),

    ("C4 flag ignores --until",
     "test_governor",
     '        flag_quota(rest[0], rest[1:], scope=scope or "account", until=until)\n',
     '        flag_quota(rest[0], rest[1:], scope=scope or "account")  # MUTANT\n',
     "TheCliCarriesTheUntil.test_flag_until_writes_it_among_the_other_arguments"),

    ("H1 the subcommand names are not printed",
     "test_governor",
     '        print("subcommands: detect | flag | check | fresh | status | clear")\n',
     '        pass  # MUTANT: names not printed\n',
     "TheCliRulesTheReadmeStates."
     "test_no_arguments_and_dash_h_exit_zero_and_name_the_subcommands"),

    ("H2 any --scope value is accepted",
     "test_governor",
     '        if scope not in ("model", "account"):\n',
     '        if False:  # MUTANT: no scope check\n',
     "TheCliRulesTheReadmeStates.test_a_bad_scope_exits_two_and_writes_nothing"),

    ("H3 check ignores --window",
     "test_governor",
     '        return 0 if is_quota_flagged(rest[0], window) else 1\n',
     '        return 0 if is_quota_flagged(rest[0]) else 1  # MUTANT: no window\n',
     "TheCliRulesTheReadmeStates.test_check_window_replaces_the_default_window"),

    ("H4 fresh ignores --window",
     "test_governor",
     '        return 0 if quota_flag_fresh(window) else 1\n',
     '        return 0 if quota_flag_fresh() else 1  # MUTANT: no window\n',
     "TheCliRulesTheReadmeStates.test_fresh_window_replaces_the_default_window"),

    ("H5 status ignores --window",
     "test_governor",
     '        entries = quota_entries(window, now=now)\n',
     '        entries = quota_entries(now=now)  # MUTANT: no window\n',
     "TheCliRulesTheReadmeStates.test_status_window_replaces_the_default_window"),

    ("H6 status swaps the newest and oldest ages",
     "test_governor",
     '            "newest_age_h": round(min(ages), 2) if ages else None,\n'
     '            "oldest_age_h": round(max(ages), 2) if ages else None,\n',
     '            "newest_age_h": round(max(ages), 2) if ages else None,  # MUTANT\n'
     '            "oldest_age_h": round(min(ages), 2) if ages else None,\n',
     "TheCliRulesTheReadmeStates.test_status_ages_are_newest_and_oldest"),

    ("H7 flag accepts a source with no model",
     "test_governor",
     '        if len(rest) < 2:\n'
     '            print("flag wants <source> <model>...", file=sys.stderr)\n',
     '        if len(rest) < 1:  # MUTANT: a source alone is enough\n'
     '            print("flag wants <source> <model>...", file=sys.stderr)\n',
     "TheCliRulesTheReadmeStates.test_flag_without_a_model_exits_two_and_writes_nothing"),

    ("H8 detect returns 0 when the model is missing",
     "test_governor",
     '            print("detect wants <source> <model>, with the text on stdin", file=sys.stderr)\n'
     '            return 2\n',
     '            print("detect wants <source> <model>, with the text on stdin", file=sys.stderr)\n'
     '            return 0  # MUTANT\n',
     "TheCliRulesTheReadmeStates.test_detect_without_a_model_exits_two_and_writes_nothing"),

    ("H9 clear with no model is allowed",
     "test_governor",
     '        if not rest:\n'
     '            print("clear wants <model>...", file=sys.stderr)\n'
     '            return 2\n',
     '        # MUTANT: no missing-model branch\n',
     "TheCliRulesTheReadmeStates.test_clear_without_a_model_exits_two_and_clears_nothing"),

    ("H10 check with no model returns 1",
     "test_governor",
     '            print("check wants <model>", file=sys.stderr)\n'
     '            return 2\n',
     '            print("check wants <model>", file=sys.stderr)\n'
     '            return 1  # MUTANT\n',
     "TheCliRulesTheReadmeStates.test_check_without_a_model_exits_two"),

    ("H11 a pool name gets a second pool line",
     "test_governor",
     '        if scope == "account" and not model.startswith("pool:"):\n',
     '        if scope == "account":  # MUTANT: no pool: condition\n',
     "TheCliRulesTheReadmeStates.test_an_account_notice_for_a_pool_name_writes_one_line"),

    ("H12 pool_of returns the bare name for a family name",
     "test_governor",
     '    return "claude" if name in _CLAUDE_ALIASES else (name or "unknown")\n',
     '    return name or "unknown"  # MUTANT: no family mapping\n',
     "TheCliRulesTheReadmeStates.test_pool_of_takes_the_text_before_the_first_dash_or_colon"),

    ("H13 a trailing Z is not read",
     "test_governor",
     '    if text[-1:] in ("Z", "z"):\n',
     '    if False:  # MUTANT: Z branch off\n',
     "TheCliRulesTheReadmeStates.test_flag_until_reads_a_trailing_z"),

    ("R1 a letter before rate is allowed to start a rate limit",
     "test_detect",
     '    r"|(?:(?<![a-z])|(?<=(?-i:[a-z]))(?=(?-i:R)))rate.?limit"\n',
     '    r"|rate.?limit"  # MUTANT: no lookbehind\n',
     "OrdinaryTextIsNotAQuota.test_words_ending_in_rate_before_limit_are_not_quotas"),

    ("R2 detect reads stdin as text again",
     "test_governor",
     '        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")\n',
     '        text = sys.stdin.read()  # MUTANT\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_detect_reads_job_output_that_is_not_utf8"),

    ("R3 the finiteness check on --window is removed",
     "test_governor",
     '        if not math.isfinite(window):\n',
     '        if False:  # MUTANT: nan and inf pass\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_a_window_that_is_not_finite_exits_2"),

    ("R4 main has no try",
     "test_governor",
     '    except Exception as exc:\n',
     '    except ZeroDivisionError as exc:  # MUTANT: a crash escapes\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_an_unexpected_error_exits_2_not_1"),

    ("R5a the CLI name check is removed",
     "test_governor",
     '        if not all(valid_name(n) for n in rest):\n',
     '        if False:  # MUTANT: no name check in flag\n',
     "TheCliIsTheInterfaceForNonPythonCallers.test_flag_refuses_a_name_with_a_space_or_parenthesis"),

    ("R5b flag_quota does not filter names",
     "test_governor",
     '    models = [m for m in dict.fromkeys(models) if valid_name(m)]\n',
     '    models = [m for m in dict.fromkeys(models) if m]  # MUTANT\n',
     "WritesDoNotErodeEachOther.test_flag_quota_skips_names_it_cannot_write"),

    ("R6 a failed replace leaves its temporary file",
     "test_governor",
     '                tmp.unlink(missing_ok=True)\n',
     '                pass  # MUTANT: temporary file kept\n',
     "WritesDoNotErodeEachOther.test_a_failed_replace_leaves_no_temporary_file"),

    ("N1 the example script drops stderr from the job output",
     "test_governor",
     "  output=$($JOB 2>&1)\n",
     "  output=$($JOB)  # MUTANT\n",
     "TheExampleScript.test_a_job_that_prints_the_notice_on_stderr_and_exits_1_is_flagged",
     "examples/nightly.sh"),

    ("N2 the example script stops when the job fails",
     "test_governor",
     "  set +e\n  output=$($JOB 2>&1)\n",
     "  output=$($JOB 2>&1)\n",
     "TheExampleScript.test_a_job_that_prints_the_notice_on_stderr_and_exits_1_is_flagged",
     "examples/nightly.sh"),

    ("N3 the example script has no demo directory",
     "test_governor",
     '  MAX_POOL_STATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/max-pool-demo.XXXXXX")\n  export MAX_POOL_STATE_DIR\n',
     '  :  # MUTANT: the demo uses the home folder\n',
     "TheExampleScript.test_the_bare_demo_leaves_the_home_folder_alone",
     "examples/nightly.sh"),

    ("N4 the example script expands file patterns",
     "test_governor",
     "set -f\n",
     "set +f  # MUTANT\n",
     "TheExampleScript.test_a_job_word_that_looks_like_a_file_pattern_is_passed_as_is",
     "examples/nightly.sh"),

    ("W1a the camelCase branch of the rate limit pattern is removed",
     "test_detect",
     r'r"|(?:(?<![a-z])|(?<=(?-i:[a-z]))(?=(?-i:R)))rate.?limit"',
     r'r"|(?<![a-z])rate.?limit"',
     "QuotaNoticesAreCaught.test_camelcase_rate_limit_names_match"),

    ("W1b the camelCase lookbehind ignores case",
     "test_detect",
     r'(?<=(?-i:[a-z]))',
     r'(?<=[a-z])',
     "OrdinaryTextIsNotAQuota.test_an_all_capital_word_ending_in_rate_is_not_a_rate_limit"),

    ("W2 the example script always exits 0",
     "test_governor",
     'done\nexit "$last_failure"\n',
     'done\nexit 0  # MUTANT\n',
     "TheExampleScript.test_a_failing_job_makes_the_script_exit_with_its_status_after_both_jobs",
     "examples/nightly.sh"),

    ("W3 the demo does not remove its temporary directory",
     "test_governor",
     "  trap 'rm -rf \"$MAX_POOL_STATE_DIR\"' EXIT\n",
     "",
     "TheExampleScript.test_the_bare_demo_removes_its_temporary_state_directory",
     "examples/nightly.sh"),

    ("W4 the import-time flag path is outside the guarded root",
     "test_detect",
     'os.environ["MAX_POOL_STATE_DIR"] = _GUARD_TMP.name',
     'os.environ["MAX_POOL_STATE_DIR"] = tempfile.TemporaryDirectory(prefix="governor-other-").name',
     "OrdinaryTextIsNotAQuota.test_ids_containing_402_or_429_are_not_quotas",
     "tests/test_detect.py"),
]

# Mutations that can fail a test only on an older Python: label -> first Python
# version on which the mutation has no effect.
ONLY_BELOW = {"H13 a trailing Z is not read": (3, 11)}


def run_selector(work: Path, test_module: str, selector: str) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(work) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", f"{test_module}.{selector}"],
            cwd=work / "tests",
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    return proc.returncode


def main() -> int:
    failed = 0
    for label, test_module, old, new, selector, *target in MUTATIONS:
        if sys.version_info >= ONLY_BELOW.get(label, (99,)):
            print(f"not applicable on this Python {label}")
            continue
        with tempfile.TemporaryDirectory(prefix="governor-mutation-") as tmp:
            work = Path(tmp)
            (work / "tests").mkdir()
            shutil.copy2(ROOT / "max_pool_governor.py", work / "max_pool_governor.py")
            for name in ("test_detect.py", "test_governor.py", "test_reset_time.py"):
                shutil.copy2(ROOT / "tests" / name, work / "tests" / name)
            shutil.copytree(ROOT / "examples", work / "examples")

            baseline = run_selector(work, test_module, selector)
            if baseline is None:
                print(f"timeout {label}")
                failed += 1
                continue
            if baseline != 0:
                print(f"survived {label}")
                failed += 1
                continue

            src_path = work / (target[0] if target else "max_pool_governor.py")
            src = src_path.read_text()
            if old not in src:
                print(f"survived {label}")
                failed += 1
                continue
            src_path.write_text(src.replace(old, new, 1))

            result = run_selector(work, test_module, selector)
            if result is None:
                print(f"timeout {label}")
                failed += 1
            elif result != 0:
                print(f"killed {label}")
            else:
                print(f"survived {label}")
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
