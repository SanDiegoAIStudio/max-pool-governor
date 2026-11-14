"""Record a usage limit notice and tell later jobs on this machine to wait until it resets."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _state_dir() -> Path:
    """Directory for the flag file and its lock file.

    MAX_POOL_STATE_DIR is read when this module loads.
    """
    raw = os.environ.get("MAX_POOL_STATE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".max-pool-governor"


FLAG_FILENAME = "limit.flag"
MAX_LIMIT_FLAG = _state_dir() / FLAG_FILENAME

# Shared shape of a short limit notice. A long document that merely mentions a
# limit must not match: that path is looks_like_quota's length guard.
LIMIT_PAT = re.compile(
    r"(?:hit|reached|exceeded)\s+your\s+.{0,24}?(?:limit|quota)"
    r"|usage\s+limit"
    r"|(?:(?<![a-z])|(?<=(?-i:[a-z]))(?=(?-i:R)))rate.?limit"
    r"|\blimit\s+reached\b"
    r"|quota\s+(?:exceeded|exhausted)"
    r"|\bout\s+of\s+(?:credits|tokens|quota)\b"
    r"|\bcredits?\s+(?:exhausted|depleted)\b"
    r"|\bout\s+of\s+extra\s+usage\b"
    r"|balance\s+exhausted"
    r"|payment\s+required"
    r"|\b(?:status|http)\s+(?:402|429)\b",
    re.I)

QUOTA_NOTICE_MAX = 300


def looks_like_quota(body: str = "", stderr: str = "") -> bool:
    """True when the text is a limit notice, not a document that mentions one.

    body is model output and counts only when it is short enough to be a notice.
    stderr is an error channel and is always matched.
    """
    if stderr and LIMIT_PAT.search(stderr):
        return True
    body = (body or "").strip()
    if body and len(body) <= QUOTA_NOTICE_MAX and LIMIT_PAT.search(body):
        return True
    return False


QUOTA_WINDOW_POOL_H = 24
QUOTA_WINDOW_TONIGHT_H = 10
QUOTA_WINDOW_COOLDOWN_H = 6

_FLAG_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\dT[\d:.]+(?:[+-]\d\d:?\d\d|Z)?)"
    r"\s+(?P<source>\S+)\s+\((?P<model>[^)]+)\)"
    r"(?:\s+until=(?P<until>\S+))?")

CLOCK_SKEW_ALLOWANCE = timedelta(minutes=5)

RESET_HORIZON_DAYS = 8


def _now() -> datetime:
    """Timezone-aware local now. Written stamps stay aware across a DST change."""
    return datetime.now().astimezone()


def _aware(when: datetime) -> datetime:
    """Naive stamps are older writes. Read them as local time."""
    return when if when.tzinfo else when.astimezone()


def _from_iso(text: str) -> datetime:
    """datetime.fromisoformat that reads a trailing Z the same on every Python."""
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _read_flag() -> list[str]:
    """Flag lines, or []. One bad byte must not discard the rest of the file."""
    try:
        return MAX_LIMIT_FLAG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse(line: str):
    """(when, source, model, until) for a parseable, non-future line, else None.

    until is an aware datetime, or None when the line has none or it does not parse.
    """
    match = _FLAG_LINE.match(line.strip())
    if not match:
        return None
    try:
        when = _aware(_from_iso(match.group("stamp")))
    except ValueError:
        return None
    if when > _now() + CLOCK_SKEW_ALLOWANCE:
        return None
    until = None
    if match.group("until"):
        try:
            until = _aware(_from_iso(match.group("until")))
        except ValueError:
            until = None
    model = match.group("model").strip()
    return (when, match.group("source"), model, until) if model else None


def _live(parsed, cutoff: datetime, now: datetime) -> bool:
    """A line with an until is live while now is before it, whatever its stamp.

    A line without one is live while its stamp is at or after the cutoff.
    """
    when, _, _, until = parsed
    if until is not None:
        return now < until
    return when >= cutoff


_CLAUDE_ALIASES = frozenset({"fable", "opus", "sonnet", "haiku"})


def pool_of(model: str) -> str:
    name = (model or "").split("-", 1)[0].split(":", 1)[0].lower()
    return "claude" if name in _CLAUDE_ALIASES else (name or "unknown")


_MODEL_SCOPED = re.compile(
    r"switch to another model"
    r"|\b(?:fable|opus|sonnet|haiku|grok|gpt)[\s-]*\d", re.I)


def quota_scope(body: str = "", stderr: str = "") -> str:
    """"model" when the notice says other models still work, else "account"."""
    return "model" if _MODEL_SCOPED.search(f"{body}\n{stderr}") else "account"


_EPOCH_SHAPE = re.compile(r"\|\s*(\d{10})(?!\d)")
_CLOCK_SHAPE = re.compile(
    r"\breset\w*\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b"
    r"(?:\s*\(([^()]*)\))?", re.I)


def _next_clock(match, now: datetime) -> datetime | None:
    """The first moment after now at which a clock in the zone reads the stated time."""
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if match.group(3).lower() == "pm" else 0)
    zone = match.group(4)
    tz = ZoneInfo(zone.strip()) if zone is not None else None
    now_ts = now.timestamp()
    today = datetime.fromtimestamp(now_ts, tz).date()
    best = None
    # Walk wall-clock days, never fixed 24 hour steps. A wall time that the zone
    # skips (the spring gap) is not a moment, so that day gives no candidate.
    for days in range(3):
        wall = datetime.combine(today + timedelta(days=days), time(hour, minute))
        for fold in (0, 1):
            ts = wall.replace(tzinfo=tz, fold=fold).timestamp()
            if ts <= now_ts:
                continue
            if datetime.fromtimestamp(ts, tz).replace(tzinfo=None) != wall:
                continue
            best = ts if best is None else min(best, ts)
        if best is not None:
            break
    if best is None:
        return None
    return datetime.fromtimestamp(best, tz) if tz else datetime.fromtimestamp(best).astimezone()


def reset_time(text, now: datetime | None = None) -> datetime | None:
    """The reset time a limit notice states, as an aware datetime, else None.

    Reads two shapes. An epoch after a pipe: "limit reached|1756500000". A
    clock time after the word reset: "resets 3am", "reset at 3:30pm
    (America/Los_Angeles)". Without a zone name the machine's local zone is
    used. The epoch is tried first, and the shape found first decides. A
    result not after now, or more than RESET_HORIZON_DAYS days after it, is
    None. A naive now is local time. Never raises.
    """
    try:
        if not isinstance(text, str):
            return None
        now = _aware(now) if now is not None else _now()
        epoch = _EPOCH_SHAPE.search(text)
        if epoch:
            found = datetime.fromtimestamp(int(epoch.group(1)), tz=timezone.utc)
        else:
            clock = _CLOCK_SHAPE.search(text)
            if not clock:
                return None
            found = _next_clock(clock, now)
        if found is None:
            return None
        if found.timestamp() <= now.timestamp():
            return None
        if found.timestamp() > now.timestamp() + RESET_HORIZON_DAYS * 86400:
            return None
        return found
    except Exception:
        return None


_NAME = re.compile(r"[^\s()]+")


def valid_name(name) -> bool:
    """True when name is a non-empty str the flag file can hold and read back.

    A name may not contain whitespace or a parenthesis, and must encode to UTF-8.
    """
    if not isinstance(name, str) or not name:
        return False
    try:
        name.encode("utf-8")
    except UnicodeError:
        return False
    return _NAME.fullmatch(name) is not None


def flag_quota(source: str, models, scope: str = "account",
               until: datetime | None = None) -> None:
    """Record that `models` are at their limit, attributed to `source`.

    Locked read, merge, and write. scope "account" also records the model's
    pool. A repeat hit refreshes the timestamp and the until. When until is
    given (a naive value is local time), every line written ends with it and
    stays live until then. Never raises.
    """
    if isinstance(models, str):
        models = [models]
    if not valid_name(source):
        return
    models = [m for m in dict.fromkeys(models) if valid_name(m)]
    if not models:
        return
    keys = []
    for model in models:
        keys.append(model)
        if scope == "account" and not model.startswith("pool:"):
            keys.append(f"pool:{pool_of(model)}")
    keys = list(dict.fromkeys(keys))
    lock_fh = None
    tmp = None
    try:
        MAX_LIMIT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(MAX_LIMIT_FLAG.with_name(MAX_LIMIT_FLAG.name + ".lock"), "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)

        now = _now()
        stamp = now.isoformat()
        suffix = f" until={_aware(until).isoformat()}" if until is not None else ""
        cutoff = now - timedelta(hours=QUOTA_WINDOW_POOL_H)
        merged = []
        for line in _read_flag():
            if not line.strip():
                continue
            parsed = _parse(line)
            if parsed is None:
                merged.append(line)
                continue
            when, line_source, model, _ = parsed
            if line_source == source and model in keys:
                continue          # superseded by the fresh stamp written below
            if _live(parsed, cutoff, now):
                merged.append(line)
        merged.extend(f"{stamp} {source} ({key}){suffix}" for key in keys)

        tmp = MAX_LIMIT_FLAG.with_name(f"{MAX_LIMIT_FLAG.name}.tmp.{os.getpid()}")
        tmp.write_text("\n".join(merged) + "\n", encoding="utf-8")
        tmp.replace(MAX_LIMIT_FLAG)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            finally:
                lock_fh.close()


def clear_quota(models=None, observed_at: datetime | None = None) -> None:
    """Drop flag lines for `models`, or every line when models is None.

    A line newer than observed_at is kept. Removes the file when nothing is
    left. Never raises.
    """
    if isinstance(models, str):
        models = [models]
    lock_fh = None
    try:
        MAX_LIMIT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(MAX_LIMIT_FLAG.with_name(MAX_LIMIT_FLAG.name + ".lock"), "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        if models is None:
            MAX_LIMIT_FLAG.unlink(missing_ok=True)
            return
        keep = set(models) | {f"pool:{pool_of(m)}" for m in models}
        cutoff = _aware(observed_at) if observed_at else None
        kept = []
        for line in _read_flag():
            if not line.strip():
                continue
            parsed = _parse(line)
            if parsed is None:
                kept.append(line)
                continue
            when, _, model, _ = parsed
            if model not in keep or (cutoff is not None and when > cutoff):
                kept.append(line)
        if not kept:
            MAX_LIMIT_FLAG.unlink(missing_ok=True)
            return
        tmp = MAX_LIMIT_FLAG.with_name(f"{MAX_LIMIT_FLAG.name}.tmp.{os.getpid()}")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(MAX_LIMIT_FLAG)
    except OSError:
        pass
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            finally:
                lock_fh.close()


def quota_entries(max_age_h: float = QUOTA_WINDOW_POOL_H, now: datetime | None = None):
    """Live flag lines as (when, source, model). Oldest first.

    A line with an until is live while now is before it, and the window does
    not apply to it. A line without one is live while its stamp is inside
    max_age_h hours.
    """
    now = _aware(now) if now else _now()
    cutoff = now - timedelta(hours=max_age_h)
    out = []
    for line in _read_flag():
        parsed = _parse(line)
        if parsed and _live(parsed, cutoff, now):
            out.append(parsed[:3])
    return sorted(out)


def quota_untils(max_age_h: float = QUOTA_WINDOW_POOL_H,
                 now: datetime | None = None) -> dict[str, datetime]:
    """Each live name that has a live line with an until, to the latest such until."""
    now = _aware(now) if now else _now()
    cutoff = now - timedelta(hours=max_age_h)
    out: dict[str, datetime] = {}
    for line in _read_flag():
        parsed = _parse(line)
        if not parsed or parsed[3] is None or not _live(parsed, cutoff, now):
            continue
        name, until = parsed[2], parsed[3]
        if name not in out or until > out[name]:
            out[name] = until
    return out


def quota_flagged(max_age_h: float = QUOTA_WINDOW_POOL_H,
                  now: datetime | None = None) -> set[str]:
    """Model names and pool markers currently flagged. Empty when clear."""
    return {model for _, _, model in quota_entries(max_age_h, now=now)}


def is_quota_flagged(model: str, max_age_h: float = QUOTA_WINDOW_POOL_H,
                     now: datetime | None = None) -> bool:
    """True when this model is unavailable by name or because its pool is."""
    if not model:
        return False
    flagged = quota_flagged(max_age_h, now=now)
    return model in flagged or f"pool:{pool_of(model)}" in flagged


def quota_flag_fresh(max_age_h: float = QUOTA_WINDOW_POOL_H,
                     now: datetime | None = None) -> bool:
    """True when something is flagged inside the window, whichever model it is.

    When the file has text but no parseable line, fall back to the file mtime
    so an unrecognised line still counts as flagged.
    """
    if quota_entries(max_age_h, now=now):
        return True
    lines = _read_flag()
    if not any(line.strip() for line in lines):
        return False
    try:
        age_h = (datetime.now().timestamp() - MAX_LIMIT_FLAG.stat().st_mtime) / 3600
    except OSError:
        return False
    return age_h < max_age_h


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("subcommands: detect | flag | check | fresh | status | clear")
        return 0
    cmd, rest = argv[0], argv[1:]

    window = QUOTA_WINDOW_POOL_H
    if "--window" in rest:
        i = rest.index("--window")
        try:
            window = float(rest[i + 1])
        except (IndexError, ValueError):
            print("--window wants a number of hours", file=sys.stderr)
            return 2
        if not math.isfinite(window):
            print("--window wants a number of hours", file=sys.stderr)
            return 2
        rest = rest[:i] + rest[i + 2:]
    scope = None
    if "--scope" in rest:
        i = rest.index("--scope")
        scope = rest[i + 1] if i + 1 < len(rest) else None
        if scope not in ("model", "account"):
            print("--scope wants model or account", file=sys.stderr)
            return 2
        rest = rest[:i] + rest[i + 2:]

    if cmd == "detect":
        if len(rest) < 2:
            print("detect wants <source> <model>, with the text on stdin", file=sys.stderr)
            return 2
        if not all(valid_name(n) for n in rest[:2]):
            print("names may not contain spaces or parentheses", file=sys.stderr)
            return 2
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        # Piped text is a log, so match it as the error channel. The length
        # guard is for model output and would drop a notice inside a long log.
        if not looks_like_quota(stderr=text):
            return 1
        used_scope = scope or quota_scope(stderr=text)
        until = reset_time(text)
        flag_quota(rest[0], rest[1], scope=used_scope, until=until)
        tail = f" until {until.isoformat()}" if until is not None else ""
        print(f"flagged {rest[1]} ({used_scope}){tail}")
        return 0

    if cmd == "flag":
        until = None
        if "--until" in rest:
            i = rest.index("--until")
            try:
                until = _aware(_from_iso(rest[i + 1]))
            except (IndexError, ValueError):
                print("--until wants an ISO time such as 2026-01-31T03:00", file=sys.stderr)
                return 2
            if until <= _now():
                print("--until must be in the future", file=sys.stderr)
                return 2
            rest = rest[:i] + rest[i + 2:]
        if len(rest) < 2:
            print("flag wants <source> <model>...", file=sys.stderr)
            return 2
        if not all(valid_name(n) for n in rest):
            print("names may not contain spaces or parentheses", file=sys.stderr)
            return 2
        flag_quota(rest[0], rest[1:], scope=scope or "account", until=until)
        return 0

    if cmd == "clear":
        if not rest:
            print("clear wants <model>...", file=sys.stderr)
            return 2
        clear_quota(rest)
        return 0

    if cmd == "check":
        if not rest:
            print("check wants <model>", file=sys.stderr)
            return 2
        return 0 if is_quota_flagged(rest[0], window) else 1

    if cmd == "fresh":
        return 0 if quota_flag_fresh(window) else 1

    if cmd == "status":
        now = _now()
        entries = quota_entries(window, now=now)
        untils = quota_untils(window, now=now)
        ages = [(now - when).total_seconds() / 3600 for when, _, _ in entries]
        print(json.dumps({
            "flagged": sorted({m for _, _, m in entries}),
            "sources": sorted({s for _, s, _ in entries}),
            "newest_age_h": round(min(ages), 2) if ages else None,
            "oldest_age_h": round(max(ages), 2) if ages else None,
            "until": {name: when.isoformat() for name, when in sorted(untils.items())},
        }))
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    """Run the command line. Any unexpected error exits 2, never 1 (1 means "no limit")."""
    try:
        return _cli(sys.argv[1:] if argv is None else argv)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
