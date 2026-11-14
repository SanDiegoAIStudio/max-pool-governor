# max-pool-governor

max-pool-governor is a small Python program that records a usage limit notice in one flag file on the machine. A scheduled job can check that flag and skip its run until the reset time the notice states, or for a window of hours when the notice states none. It is for anyone who runs unattended agent jobs against a subscription plan and wants those jobs to stop launching when the plan is exhausted.

## Quick start

From a fresh clone, in the repository root:

```
python3 -m unittest discover -s tests -q
```

```
MAX_POOL_STATE_DIR="$(mktemp -d)" sh examples/nightly.sh
```

The second command needs no account. It uses a temporary state directory and the sample notice in examples/limit-notice.txt.

The program uses only the Python standard library, and it needs Python 3.9 or later because it imports `zoneinfo`. On the machine where it was written, `/usr/bin/python3 --version` prints `Python 3.9.6` and `python3 --version` prints `Python 3.14.7`. The tests and the example ran on both.

## Example

This was run from the repository root:

```
MAX_POOL_STATE_DIR="$(mktemp -d)" sh examples/nightly.sh
```

The script printed:

```
job 1: running
You've hit your usage limit. Your limit will reset at 3am.
flagged pool:default (account) until 2026-11-14T03:00:00-08:00
job 1: limit notice found, flag raised
job 2: skipped: usage limit is flagged, so this job did not run
```

The shell reported exit code 0. The run was at 15:16 on 2026-11-13 in a machine zone that is 8 hours behind UTC. The first job printed the sample notice, detect read "3am" from it, and the flag line got `until=2026-11-14T03:00:00-08:00`. The second job did not run. On your machine the date and the offset will follow your clock and your zone. The job runs again once the clock passes that time.

Set JOB to the command you want to guard. The script splits JOB on spaces and never expands a file pattern in it. It reads what the job prints on stdout and on stderr, and a job that exits with an error still has its output read and gets a line saying so. The default is `cat examples/limit-notice.txt`. POOL is the name stored in the flag. The default is `pool:default`. SOURCE is the name recorded next to that line. The default is `nightly`. When neither JOB nor MAX_POOL_STATE_DIR is set, the script uses a new temporary state directory and says so on stderr, so the demo never writes a flag into your home folder.

## Commands

Run `python3 max_pool_governor.py` or `python3 max_pool_governor.py -h`. Both exit 0 and print the subcommand names: detect, flag, check, fresh, status, clear.

`detect SOURCE MODEL` reads the job output on stdin. Bytes that are not UTF-8 are replaced, so binary output cannot stop it. `--scope model` or `--scope account` is optional. With no `--scope`, detect calls `quota_scope` on the text. That returns `model` when the notice says to switch models or matches the model-scoped pattern in the module, and `account` otherwise. Exit 0 when the text is a limit notice: it reads the reset time from the text, writes the flag, and prints `flagged MODEL (scope)` when no reset time was read or `flagged MODEL (scope) until ISO` when one was. Exit 1 when the text is not a notice, and it writes nothing. Exit 2 when the source or model is missing or contains a space or a parenthesis, when `--scope` is not `model` or `account`, or when `--window` is not a number of hours.

`flag SOURCE MODEL [MODEL ...]` records those names. The default scope is account. `--scope model` records only the names you passed. `--until ISO` states the reset time yourself, for example `flag nightly plan-east --until 2026-01-31T03:00`. The value is read with `datetime.fromisoformat`, and a value with no zone is local time. A trailing `Z` is read as UTC. Exit 0 when the arguments are valid. Exit 2 when the source or a model is missing or contains a space or a parenthesis, or when `--scope` is not `model` or `account`. Exit 2 with `--until wants an ISO time such as 2026-01-31T03:00` on stderr when the value is missing or does not parse. Exit 2 with `--until must be in the future` on stderr when the time is not later than now. After an exit 2 nothing is written.

`check MODEL` exits 0 when that model is unavailable and 1 when it is available. Exit 2 when the model is missing or `--window` is not a number. `--window HOURS` replaces the default window for this call.

`fresh` exits 0 when any flag line is live and 1 when none is. When the file has text but no line is live, it falls back to the time the file was last changed and exits 0 if that is inside the window. Exit 2 when `--window` is not a number.

`status` prints one JSON object and exits 0. The keys are `flagged`, `sources`, `newest_age_h`, `oldest_age_h`, and `until`. When nothing is flagged, the two lists are empty and both age fields are null. `until` is an object from each flagged name that carries a reset time to that time as an ISO string, and it is `{}` when no name carries one. Exit 2 when `--window` is not a number.

`clear MODEL [MODEL ...]` removes the lines for those names and the pool line of each, so `clear plan-east` also removes `pool:plan`, and exits 0. Exit 2 when no model is given.

An unknown subcommand exits 2 and names the subcommand on stderr. Any other error exits 2 with `error:` and a description on stderr, so a crash is never read as exit 1.

`--window`, `--scope` and `--until` may sit among the other arguments. `--window` must be a finite number of hours. The default window is `QUOTA_WINDOW_POOL_H`, which is 24 hours.

## How it works

All state lives in one directory. `MAX_POOL_STATE_DIR` names it. When that variable is unset, the directory is `~/.max-pool-governor`. The program reads the variable when the module loads.

The flag file is `limit.flag`. While a write holds the lock it uses `limit.flag.lock` in the same directory. Each line is the time the line was written, a source name, and a model or pool name in parentheses, then optionally `until=` and a time with its zone offset. A write locks the file, reads the existing lines, merges the new ones, and replaces the file. A second writer does not erase the first. A repeat hit from the same source replaces its line, and so replaces the until as well.

`looks_like_quota` matches a short notice. Text passed as model output counts only when it is at most `QUOTA_NOTICE_MAX` characters, which is 300. A longer document that merely mentions a limit does not match. `detect` matches the piped text as an error channel, so a notice inside a long log still counts.

An account-level notice records the name you passed and, unless that name already starts with `pool:`, the pool taken from it. A later `check` of another name on that pool is also a hit. A model-level notice records only the name you passed. `pool_of` in the module decides the pool. It takes the text before the first dash or colon, in lower case, so `plan-east` and `plan-west` share the pool `plan`; the names `fable`, `opus`, `sonnet` and `haiku` share the pool `claude`, and an empty name gives `unknown`. When a reset time is known, the name line and the pool line carry the same one.

`reset_time` reads the reset time out of a notice. It reads two shapes and no others.

- An epoch after a pipe: ten digits of seconds since 1970 in UTC, as in `usage limit reached|1756500000`.
- A clock time directly after the word reset: `resets 3am`, `Your limit will reset at 3am.`, `resets 3:30pm`, or `reset at 3pm (America/Los_Angeles)`. The hour is 1 to 12 and the minutes are 00 to 59. The result is the first moment after now at which a clock in that zone reads that time, today or tomorrow, and it reads that time on the wall clock even across a daylight saving change. With a zone name in parentheses the program uses that zone, and without one it uses the machine's local zone. `resets Nov 20, 9am` has a date between the word and the time, so it is not read.

The epoch is tried first, and the first shape found in the text decides. A time that is not later than now, or is more than 8 days ahead (`RESET_HORIZON_DAYS`), is thrown away, and the notice then counts as one with no readable time. The program never raises on odd input.

A line with a reset time ignores the window. It is live until that time and not a moment longer, even when it was written more than 24 hours ago, and it stops at that time even when it was written a minute ago. A line with no reset time expires on its own from the time it was written: a reader ignores it once it is older than the window you pass. The other windows in the module are `QUOTA_WINDOW_TONIGHT_H` at 10 hours and `QUOTA_WINDOW_COOLDOWN_H` at 6 hours. A line whose `until=` value cannot be read is treated as a line with none. When another source writes, it keeps your line while the line is live by this rule, using the 24 hour window for a line without a reset time, and drops it after.

The sample notice says the limit resets at 3am. The program reads that as the next 3am on the machine's clock and zone, so a job that checks before that 3am is skipped and a job that checks after it runs again.

## Limits

It sets no order among jobs. It reads only the notices it is shown, which means the text you pass to `detect`. A job you do not wrap is invisible to it.

Only the two shapes above are read. Any other notice, such as one that gives a date, falls back to the window. When you know the time, `flag --until` lets you state it yourself, and the 8 day guard does not apply to a time you type.

A zone name needs the machine's time zone database. A name that does not load makes the notice a notice with no readable time, and the program does not fall back to local time for it.

The program uses the `fcntl` module for file locks. That module exists on macOS and Linux and does not exist on Windows, so the program does not run on Windows. The tests were run on macOS.

## Where this came from

This is the limit governor from a private module that the author's unattended jobs call before a model run. The job checks the flag, and the same program writes the flag when a run comes back with a limit notice. In the private original a separate job records each pool's reset time. This copy reads it from the notice itself. The rest of that module stays private.

## License

This project is under the MIT license. The terms are in the LICENSE file.

## Built at San Diego AI Studio

This tool comes out of San Diego AI Studio, the builders' community in San Diego. The studio's projects are at https://sandiegoaistudio.com/projects.
