# `live` — design

Stream long-lived command output to coding agents. `live run <cmd>` runs `<cmd>` under a PTY, mirrors output to the terminal, and records the bytes to disk in the nearest `.live/`. Agents read with `live cat`, `live tail`, or resumable `live tail --since-line N`, piping to `grep`/`awk` as needed.

The recorder is the sole writer per session. Read verbs hold no per-process state and piggyback lifecycle sweeps. No daemon, no broker, no persistent server.

Python 3.14+, POSIX-only (Linux, macOS, WSL).

## CLI

| Verb                                                                                            | Purpose                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-n NAME] [--] <cmd…>`                                                                | Wrap `<cmd>` under a PTY, mirror to stdout, record to disk.                                                                                                                                                                     |
| `live ls [-n NAME] [-a] [--json]`                                                               | List sessions in scope. `-a` / `--all` includes exited; `--json` emits NDJSON with the full per-session field set.                                                                                                              |
| `live cat [-v] [--strip-ansi\|--raw] <SELECTOR>`                                                | Concatenate all `stream.*.log` for the session. `-v` adds stderr metadata. `--strip-ansi` removes ANSI escapes; `--raw` keeps them. Default: strip when stdout isn't a TTY.                                                     |
| `live tail [-f] [-v] [--strip-ansi\|--raw] [-n LINES \| -c BYTES \| --since-line N] <SELECTOR>` | Tail. Unix `tail` flag conventions; `-f` follows new lines until exit; `--since-line N` outputs lines after `N` for resumable polling, implies `-v`, and always strips ANSI. ANSI handling otherwise matches `cat`.            |
| `live rm [-f] [--all-exited] <SELECTOR…>`                                                       | Delete sessions. `-f` SIGTERMs running recorders and ignores nonexistent. `--all-exited` removes every dead session in scope. Per-selector errors don't abort the batch; nonzero exit if any failed.                            |
| `live init`                                                                                     | Create `.live/`, `.live/sessions/`, and `.live/.gitignore` in cwd. Idempotent.                                                                                                                                                  |
| `live llms.txt`                                                                                 | Print a token-minimal agent guide for `live tail --since-line` polling.                                                                                                                                                         |
| `live completion <bash\|zsh\|fish>`                                                             | Print the shell completion script.                                                                                                                                                                                              |

`live`, `live -h`: usage. `live <verb> -h`: per-verb help. `live --version`.

### Scope

Discovery is git-style: walk up from cwd to the nearest `.live/`. That single directory is the scope for every verb — read and write alike. If walk-up reaches `/` without finding one, scope is `~/.live/` (auto-created on first use). No recursive descent, no filesystem crawl per command. To act on `~/.live/` from inside a project, `cd ~` first.

### Selectors

A selector is a single positional token, resolved like a git ref — names first, hash prefix as fallback:

1. **NAME** — any in-scope session with `meta.name == token`. For `cat` / `tail`, the most recent match wins. For `rm`, every match is selected (use a UUID prefix to target one).
2. **UUID prefix** — fallthrough when no NAME matches. Unique match required; ambiguous → error listing candidates.

No match → error. Selectors are required on `cat`, `tail`, `rm`; `rm` accepts multiple and `--all-exited` substitutes for selectors. Use `--` to pass a token starting with `-` (e.g. `live tail -- -my-session`).

"Most recent" = UUIDv7 lex-descending sort, top result.

### Verbose output

`cat` and `tail` accept `-v` / `--verbose`. With `-v`, stdout is unchanged and stderr carries metadata lines; without it, stderr is silent on success. `--since-line` implies `-v`.

All verbose lines are prefixed `live: `. The trailing line of any verbose read is the identity/cursor stamp:

```
live: id=<uuid> at-line=<L>
```

`<uuid>` is the resolved session's UUID; `<L>` is its `lastLine` at the moment the read completed. Agents using `--since-line` pass `<L>` as the next cursor and compare `<uuid>` against the previously seen one to detect a NAME selector drifting to a new session — reset the cursor to `0` on UUID change.

Additional stderr lines may precede the trailer, in this order when multiple apply:

1. Gap — retention dropped lines, or `cat` reading a session whose oldest segment has been unlinked: `live: dropped <k> lines (since=<N>, first retained=<firstLine>)`. For `cat`, `<N>` is `0`.
2. Cursor ahead — `tail --since-line` with `N > lastLine`, likely session swap: `live: since-line=<N> > at-line=<L>; check id`.
3. Partial line — active stream has unindexed trailing bytes (`\r`-only progress, prompt waiting on input): `live: partial-line bytes=<k> age=<s>`. The partial bytes are emitted to stdout after the last indexed line.
4. Hung — recorder still running but quiet for too long: `live: status=hung last-activity=<s>`.
5. Exited — graceful: `live: exit-code=<N>`. Torn recordings emit `live: exit=inconsistent` instead. Omitted for running sessions.

Errors are always printed regardless of `-v`, with the same `live: ` prefix.

Exit codes: `0` success; `1` runtime error (I/O, config, recorder failure); `2` usage error (bad flag, missing session, ambiguous selector). Session-not-found stderr: `live: no such session: <selector>`.

### `live tail --since-line`

Resumable polling for agents. Outputs lines with `n > N` to stdout. Mutually exclusive with `-n` / `-c`, implies `-v`.

- Caught up (`N == lastLine`): empty stdout, trailer, exit 0.
- Cursor ahead (`N > lastLine`): see [Verbose output](#verbose-output).
- Gap (`N + 1 < firstLine`): see [Verbose output](#verbose-output); stdout starts from the oldest retained line. Exit 0.
- Partial line: trailing unindexed bytes appear in stdout after the last indexed line; `live: partial-line …` precedes the trailer.
- Hung session: stdout drains whatever's newly indexed, then `live: status=hung …` appears before the trailer. The session is still alive — polling agents can continue but should warn the user; a subsequent poll either resumes producing lines or eventually reports an exit.
- Exited session: drained like any live session — tail emits the remaining lines, then the exit trailer (`live: exit-code=<N>` or `live: exit=inconsistent`). Polling loops can stop on that trailer.

### `live tail -f`

Follow mode for humans. Emits the initial slice (`-n LINES`, `-c BYTES`, `--since-line N`, or the default last-10 lines), then streams each new line as it's indexed. Exits cleanly on graceful or torn exit; exits without a trailer on `SIGINT`. Composes with `--since-line`, though agents should prefer one-shot `--since-line` polls (`-f` holds a process open, which agents typically don't want).

### `live ls`

Lists sessions in scope, newest-first (UUIDv7 lex desc). Running only by default; `-a` / `--all` includes exited. `-n NAME` filters to that label.

Default output: human columns — id-prefix, status, name, command. The name column is always rendered; sessions started without `-n NAME` show `-`. `--json` emits NDJSON, one object per session, with the full field set:

- `id`, `command`, `cwd`, `startedAt`
- `name?` — present iff started with `-n NAME`
- `status` — `"running"` (recorder alive, fresh activity) | `"hung"` (recorder alive, stale activity) | `"exited"` (graceful) | `"inconsistent"` (torn recording)
- `exitedAt?`, `exitCode?` — present after the recorder dies; `exitCode` only on graceful exit
- `path` — absolute session directory
- `firstSegment`, `lastSegment` — both `0` for a freshly-started session
- `firstLine`, `lastLine`, `count` — `0`/`0`/`0` until the first complete line; otherwise `count = lastLine − firstLine + 1`
- `lastActivity` — seconds-since-epoch mtime of the active idx (float)

### `live llms.txt`

Prints a snippet for users to add to their agent docs. The literal payload:

```
This project uses `live`, a CLI streamer.

List available sessions:
  live ls [-a] [--json]

Read output from a live session:
  live tail --since-line N <SELECTOR>
    stdout:  lines with n>N
    trailer: live: id=<uuid> at-line=<L>
    resume:  next N = <L>; reset N=0 if <uuid> changes
    stop:    stderr has "live: exit-code=" or "live: exit=inconsistent"
    hung:    stderr "live: status=hung last-activity=<s>" (still alive, just stalled)
    gap:     stderr "live: dropped <k> lines (since=<N>, first retained=<F>)"
    partial: stderr "live: partial-line bytes=<k> age=<s>"

  SELECTOR: UUID prefix or NAME (newest match)

Pipe output from `live tail` to other tools like `grep`.
```

## On-disk layout

Sessions live under `<root>/.live/sessions/<uuid>/`. `~/.live/` is auto-created on first use and hosts `config.json`.

```
<project>/.live/
  .gitignore                # written by `live init`; ignores `sessions/`
  config.json               # optional per-project override
  sessions/
    <uuid>/
      meta.json             # session metadata (command, name, exit info); writer-only, replaced atomically
      process.lock          # held by the recorder for its lifetime; presence + lock = liveness
      deadAt                # post-mortem marker; mtime = TTL clock, content = verdict
      stream.NNNN.log       # raw PTY bytes, zero-padded segment number
      lines.NNNN.idx        # binary line index, one fixed-size record per complete line
```

The recorder appends only to the highest-numbered `stream`/`lines` pair. Frozen segments are immutable until retention unlinks them. Session IDs are UUIDv7: 36-char hyphenated hex, lex-monotonic = chronological.

## Recording

`live run <cmd>` is transparent — keystrokes, prompts, Ctrl-C, and resize reach `<cmd>` directly. The recorder PTY-wraps the child, mirrors its bytes to stdout, and appends them to the active stream segment. On every `\n`, it appends a record `(n, t)` to the parallel index — `n` is the absolute line number across the session's lifetime; `t` is the timestamp of the line's first byte. Trailing bytes without a newline are surfaced to readers as a "partial line" but never indexed until the newline arrives.

The recorder is the sole writer per session. It holds an exclusive `flock` on `process.lock` for its lifetime; that lock IS the liveness signal. Readers and sweepers probe by trying to acquire the same lock non-blocking — success means the recorder is gone.

**Prefix invariant.** The stream is always one complete line ahead of, or equal to, the index — never the other way around. A crash leaves one extra complete line in stream with no matching index record; sweepers detect this and stamp the session `inconsistent`.

**Idle heartbeat.** The recorder advances the active idx mtime at least every `heartbeatSec`. Staleness past `3 × heartbeatSec` while the lock is still held = `hung`.

**Signals.** `SIGWINCH` propagates window size to the child PTY. `SIGTERM`/`SIGHUP` forward to the child and run the graceful-exit path. `SIGINT` is forwarded the same way only when stdin isn't a TTY — with a TTY, line discipline routes ^C to the child's pgroup directly.

`live run` exits with the child's exit code, or `128 + signum` if it died on a signal.

## Segments and retention

The recorder rotates segments at line boundaries when the active segment passes `segmentKb`; lines never split, so an oversize line produces a fat segment. After each rotation, if total `stream.*.log` bytes exceed `maxKb`, the recorder unlinks the lowest-numbered pair — stream first, then index, so a reader's listing never sees an orphan idx.

Line numbers (`n`) are absolute across the session's lifetime. Retention deletes but never renumbers; a reader whose cursor falls behind the oldest retained line learns about the gap via the verbose `dropped` stderr line.

Readers re-list segments on every call and tolerate `ENOENT` from rotation/retention races.

## Liveness and cleanup

**Liveness** = recorder holds `flock(process.lock)`. Recorder open + lock + write pid happens before any other file in the session directory appears, so readers and sweepers can tell "starting" from "dead" by the lock file's presence alone.

**Sweep** runs on every verb that touches sessions. For each dead-but-unmarked session, it creates `deadAt` (exclusive create) carrying the consistency verdict. Once `deadAt`'s mtime is older than `ttlDays × 86400 s`, the sweep deletes the session directory. Concurrent sweepers are race-safe.

`deadAt` is empty for graceful exits and contains `inconsistent` for torn recordings. Its mtime is the TTL clock — live sessions are never cleaned.

**Graceful exit.** On normal child exit, the recorder updates `meta.json`, creates `deadAt`, then releases the lock — in that order, so no sweep can race in and stamp a differing verdict.

**`live rm -f` on a running session** SIGTERMs the recorder, waits briefly for `flock` release, SIGKILLs if needed, then unlinks the directory.

## Configuration

`~/.live/config.json` is auto-created with defaults. Any `.live/` may carry its own `config.json` to override fields. Layering: per-`.live/` over home over compiled defaults. Partial files are valid; unknown keys are ignored; out-of-range or wrong-typed fields fall back to the layer below.

```json
{ "ttlDays": 7, "maxKb": 512, "segmentKb": 64, "heartbeatSec": 30 }
```

Validation: `ttlDays >= 0`, `maxKb > 0`, `segmentKb > 0`, `heartbeatSec > 0`, all integers. Malformed per-project config is logged and ignored; malformed home config warns and falls back to defaults.

## Distribution

Python 3.14+. Zero runtime dependencies — PTY, flock, ioctl, signals, atomic rename, struct packing, JSON, UUIDv7, and the kqueue/inotify primitives that power `tail -f` are all stdlib. PyPI: `astralarya-live`. Install via `pipx install astralarya-live` or `uv tool install astralarya-live`.

## Defaults

| Thing        | Value                                                          |
| ------------ | -------------------------------------------------------------- |
| Scope        | walk up from cwd to nearest `.live/`; fallback `~/.live/`      |
| Capture      | PTY, merged stdout + stderr                                    |
| TTL          | 7 days from `deadAt` mtime, dead sessions only                 |
| Segment size | 64 KB rotation threshold; lines never split                    |
| Retention    | 512 KB total per session; oldest segments unlinked when over   |
| Liveness     | held flock on `process.lock`                                   |
| Heartbeat    | active idx mtime advanced every 30 s (`heartbeatSec`)          |
| Config       | `~/.live/config.json` plus optional per-`.live/` overrides     |
