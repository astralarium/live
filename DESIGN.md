# `live` — design

Stream long-lived command output to coding agents. `live run <cmd>` runs `<cmd>` under a PTY, mirrors output to the terminal, and records the bytes to disk in the nearest `.live/`. Agents read with `live cat`, `live tail`, or resumable `live tail --since-line N`, piping to `grep`/`awk` as needed.

The recorder is the sole writer. Read verbs hold no per-process state and piggyback lifecycle sweeps. No daemon, no broker, no persistent server.

Python 3.14+, POSIX-only (Linux, macOS, WSL).

## CLI

| Verb                                                                                            | Purpose                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-n NAME] [--] <cmd…>`                                                                | Wrap `<cmd>` under a PTY, mirror to stdout, record to disk.                                                                                                                                                                     |
| `live ls [-n NAME] [-a] [--json]`                                                               | List sessions in scope. `-a` / `--all` includes exited; `--json` emits NDJSON with the full per-session field set.                                                                                                              |
| `live cat [-v] [--strip-ansi\|--raw] <SELECTOR>`                                                | Concatenate all `stream.*.log` for the session. `-v` adds stderr metadata. `--strip-ansi` removes ANSI escapes; `--raw` keeps them. Default: strip when stdout isn't a TTY.                                                     |
| `live tail [-f] [-v] [--strip-ansi\|--raw] [-n LINES \| -c BYTES \| --since-line N] <SELECTOR>` | Tail. Unix `tail` flag conventions; `-f` follows new lines until exit; `--since-line N` outputs lines after `N` for resumable polling, implies `-v`, and always strips ANSI. ANSI handling otherwise matches `cat`.            |
| `live rm [-f] [--all-exited] <SELECTOR…>`                                                       | Delete sessions. `-f` SIGTERMs running recorders and ignores nonexistent. `--all-exited` removes every dead session in scope. Per-selector errors don't abort the batch; nonzero exit if any failed.                            |
| `live init`                                                                                     | Create `.live/` and `.live/sessions/` (mode `0700`) plus `.live/.gitignore` in cwd. Idempotent.                                                                                                                                 |
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

### `live run` argv

`live run` consumes `-n NAME` / `--name=NAME`; everything from the first non-flag token (or after `--`) is the opaque wrapped command.

### Verbose output

`cat` and `tail` accept `-v` / `--verbose`. With `-v`, stdout is unchanged and stderr carries metadata lines; without it, stderr is silent on success. `--since-line` implies `-v`.

All verbose lines are prefixed `live: `. The trailing line of any verbose read is the identity/cursor stamp:

```
live: id=<uuid> at-line=<L>
```

`<uuid>` is the resolved session's UUID; `<L>` is its `lastLine` at the moment the read completed. Agents using `--since-line` pass `<L>` as the next cursor and compare `<uuid>` against the previously seen one to detect a NAME selector drifting to a new session — reset the cursor to `0` on UUID change.

Additional stderr lines may precede the trailer, in this order when multiple apply:

1. Gap (`N + 1 < firstLine` because retention dropped lines, or `cat` reading a session whose oldest segment has been unlinked): `live: dropped <k> lines (since=<N>, first retained=<firstLine>)`. For `cat`, `<N>` is `0`.
2. Cursor ahead (`tail --since-line` with `N > lastLine`, likely session swap): `live: since-line=<N> > at-line=<L>; check id`.
3. Partial line (active stream has unindexed trailing bytes — `\r`-only progress, prompt waiting on input): `live: partial-line bytes=<k> age=<s>`. The partial bytes are emitted to stdout after the last indexed line.
4. Hung (flock held, `now − lastActivity > 3 × heartbeatSec`): `live: status=hung last-activity=<s>`.
5. Exited (graceful): `live: exit-code=<N>`. Torn recordings (`deadAt = "inconsistent"`) emit `live: exit=inconsistent` instead. Omitted for running sessions.

Errors are always printed regardless of `-v`, with the same `live: ` prefix.

Exit codes: `0` success; `1` runtime error (I/O, config, recorder failure); `2` usage error (bad flag, missing session, ambiguous selector). Session-not-found stderr: `live: no such session: <selector>`.

### `live tail --since-line`

Resumable polling for agents. Outputs lines with `n > N` to stdout. Mutually exclusive with `-n` / `-c`, implies `-v`.

- Caught up (`N == lastLine`): empty stdout, trailer, exit 0.
- Cursor ahead (`N > lastLine`): see [Verbose output](#verbose-output).
- Gap (`N + 1 < firstLine`): see [Verbose output](#verbose-output); stdout starts from the oldest retained line. Exit 0.
- Partial line: trailing unindexed bytes appear in stdout after the last indexed line; `live: partial-line …` precedes the trailer.
- Hung session: stdout drains whatever's newly indexed, then `live: status=hung …` appears before the trailer. The session is still alive (flock held) — polling agents can continue but should warn the user; a subsequent poll either resumes producing lines or eventually reports an exit.
- Exited session: drained like any live session — tail emits the remaining lines, then the exit trailer (`live: exit-code=<N>` or `live: exit=inconsistent`). Polling loops can stop on that trailer.

### `live tail -f`

Follow mode for humans. Emit the initial slice (`-n LINES`, `-c BYTES`, `--since-line N`, or the default last-10 lines), then watch the active `lines.*.idx` and stream each new line as it's indexed.

- Event-driven, no polling: `select.kqueue` on macOS (watch the idx fd for `NOTE_WRITE | NOTE_EXTEND`), inotify on Linux/WSL via a small `ctypes` shim (`IN_MODIFY` on the idx path).
- Rotation: on the same watch, listen for the parent dir's `NOTE_WRITE` (kqueue) / `IN_CREATE` (inotify) to detect a new `lines.NNNN+1.idx`; re-arm the watch on the new active segment.
- On hung detection (staleness crosses `3 × heartbeatSec` with no event): emit the hung stderr line once, keep watching. A heartbeat or write clears the hung state.
- On graceful or torn exit: drain remaining lines, emit the exit trailer, exit 0.
- On `SIGINT` from the user's terminal: clean exit without trailer.
- Composes with `--since-line`: starts from the cursor, then follows. (Agents should still use one-shot `--since-line` polls; `-f` holds a process open, which agents typically don't want.)

### `live ls`

Lists sessions in scope, newest-first (UUIDv7 lex desc). Running only by default; `-a` / `--all` includes exited. `-n NAME` filters to that label.

Default output: human columns — id-prefix, status, name, command. The name column is always rendered; sessions started without `-n NAME` show `-`. `--json` emits NDJSON, one object per session, with the full field set:

- `id`, `command`, `cwd`, `startedAt`
- `name?` — present iff started with `-n NAME`
- `status` — `"running"` (flock held, fresh activity) | `"hung"` (flock held, `now − lastActivity > 3 × heartbeatSec`) | `"exited"` (graceful) | `"inconsistent"` (torn recording, from `deadAt` content)
- `exitedAt?` — see [`meta.json`](#metajson) precedence
- `exitCode?` — present on graceful exit
- `path` — absolute session directory
- `firstSegment`, `lastSegment` — both `0` for a freshly-started session
- `firstLine`, `lastLine`, `count` — `0`/`0`/`0` until the first complete line; otherwise `count = lastLine − firstLine + 1`
- `lastActivity` — seconds-since-epoch mtime of the active `lines.*.idx` (float)

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
      meta.json
      process.lock          # flock'd exclusive for the recorder's lifetime; content = recorder pid
      deadAt                # post-mortem marker; mtime = TTL clock, content = verdict
      stream.0000.log       # raw PTY bytes
      stream.0001.log
      lines.0000.idx        # binary; 16-byte records, parallel to stream.NNNN.log
      lines.0001.idx
```

`stream` and `lines` are zero-padded numbered segments. The recorder appends only to the highest-numbered pair; frozen segments are immutable until retention unlinks them.

### Session IDs

UUIDv7 (RFC 9562) via stdlib `uuid.uuid7()`. Standard 36-char hyphenated hex; lex-monotonic = chronological.

### `meta.json`

```json
{
  "id": "0190131a-8c00-7000-8000-000000000000",
  "command": ["uv", "run", "dev"],
  "cwd": "/abs/path",
  "name": "dev",
  "startedAt": 1717200000.0,
  "exitedAt": null,
  "exitCode": null
}
```

- `name` present only when started with `-n NAME`.
- Timestamps are float seconds since epoch: `time.time()`.

Writer-only. Written at session start and graceful exit. Atomic via `NamedTemporaryFile(dir=session_dir, delete=False)` + `fsync` + `os.replace` — `dir=session_dir` keeps temp and target on the same filesystem so the rename is atomic.

Segment list and watermarks are derived from the filesystem:

- Segments: sort `stream.NNNN.log` / `lines.NNNN.idx` filenames numerically. `firstSegment` / `lastSegment` are the min/max.
- `firstLine` = first record's `n` in `lines.<firstSegment>.idx`.
- `lastLine` = unpack the trailing 16 bytes of `lines.<lastSegment>.idx`; if that segment's idx is empty, walk back one segment.
- `count` = `lastLine − firstLine + 1`.
- No segment has any record (just-started session): `firstLine = lastLine = count = 0`.

`exitedAt` precedence: `meta.exitedAt` (graceful, exact) → mtime of the active `lines.*.idx` (crash; bounded within `heartbeatSec`) → `mtime(deadAt)` (fallback).

### `lines.NNNN.idx`

Append-only binary, 16-byte records: `struct.pack(">Qd", n, t)` — uint64 BE line number, float64 BE timestamp (seconds since epoch).

## Recording

Goal: `live <cmd>` is transparent — keystrokes, prompts, Ctrl-C, and resize reach `<cmd>` directly.

### Startup order

1. `mkdir(session_dir, mode=0o700)`. Session contents frequently include secrets (env vars, API keys printed by failing tools); the directory is owner-only.
2. `open(process.lock, O_WRONLY | O_CREAT, 0o600)` and `flock(LOCK_EX | LOCK_NB)`. Liveness is claimed before any other file appears.
3. Write the pid into `process.lock`.
4. Create empty `stream.0000.log` and `lines.0000.idx`.
5. Atomically write `meta.json`.
6. `pty.fork()` and `os.execvp` in the child.

Readers skip any session missing `meta.json` (treat as starting). The sweep predicate skips any session whose `process.lock` doesn't exist yet (see [Sweep](#sweep)) — together these cover the steps 1–5 startup window.

### PTY and signals

Child: `pty.fork()` → `os.execvp`. Parent selects on `[0, master_fd, wakeup_fd]`. If `os.isatty(0)`, stdin is put in raw mode (`tty.setraw(0)`, saved and restored on exit) and the initial PTY size is seeded with `TIOCGWINSZ` on stdin → `TIOCSWINSZ` on `master_fd`; otherwise raw mode is skipped, the PTY keeps its default 80×24, and it still works with redirected/piped stdin.

- `master_fd` → write to `sys.stdout.buffer`, append to `stream.NNNN.log`, update line index.
- stdin → write to `master_fd`. The PTY's line discipline routes ^C to the child's pgroup.
- `wakeup_fd` (`signal.set_wakeup_fd`) delivers signals: `SIGWINCH` propagates size via `TIOCGWINSZ` / `TIOCSWINSZ`; `SIGTERM` / `SIGHUP` forward to the child and graceful-exit. `SIGINT` gets a handler only when stdin is non-TTY — with a TTY, the line discipline routes ^C to the child's pgroup and the parent never sees it; with redirected stdin, the handler forwards `SIGINT` to the child and runs the graceful-exit path.

### Line indexing

- On the first byte of a new line, capture `t = time.time()`.
- On `\n`, append `struct.pack(">Qd", n, t)` to `lines.NNNN.idx`.
- Trailing partial lines are not recorded until the newline arrives.

`n` is absolute across the session's lifetime. Retention deletes segments but never renumbers.

Readers may expose trailing unindexed bytes from the active stream as a "partial line" — surfaces `\r`-only progress and prompts without a trailing `\n`. See [Verbose output](#verbose-output).

### Idle heartbeat

The recorder advances `lines.<lastSegment>.idx`'s mtime at least every `heartbeatSec`. The select loop uses that as its timeout; on idle wake-up, `os.utime` if no write touched the file within the interval.

flock detects process death; mtime staleness on the active idx detects a hung process (`status: "hung"` when `now − lastActivity > 3 × heartbeatSec`).

### Write-order invariant

Each line: stream byte append, then index record append. **Prefix invariant**: index records are always a prefix of complete lines in stream. A crash leaves one extra complete line in stream with no index record, never the reverse.

If any `stream.*.log` or `lines.*.idx` write raises `OSError` mid-session, the recorder kills the PTY and stamps `deadAt` with `inconsistent` before exiting nonzero.

## Segments and retention

Configurable per `.live/`:

- `segmentKb` (default 64) — rotate after a completed line carries the active segment past this.
- `maxKb` (default 512) — total retained `stream.*.log` bytes per session.

**Rotation.** When the active segment hits `segmentKb` at a line boundary, close it and open a new pair. Lines never split; an oversize line produces a fat segment and rotates after. Pure filesystem op — no meta write.

**Reader tolerance.** Readers may briefly see one of the new pair before the other (recorder creates `stream.NNNN+1.log` then `lines.NNNN+1.idx`). Treat `ENOENT` on the highest-numbered segment's files as empty and walk back one for `lastLine`. `ENOENT` on any lower segment is a real error.

**Retention.** After each rotation, sum `stream.*.log` bytes. While over `maxKb`, `os.unlink` the lowest-numbered pair — `stream.NNNN.log` first, then `lines.NNNN.idx`. Stream-first removes the pair from readers' `stream.*.log` glob atomically.

**Reader race.** If retention unlinks the lowest pair between a reader's listing and its `open()`, `open()` returns `ENOENT`; readers re-list and continue from the new oldest. The next `tail --since-line` poll surfaces dropped tracked lines via the `live: dropped <k> lines …` stderr line.

## Liveness and cleanup

**Liveness** = recorder holds an exclusive `fcntl.flock` on `process.lock`. Recorder opens with `O_WRONLY | O_CREAT`, takes `flock(LOCK_EX | LOCK_NB)`, writes its pid as decimal ASCII, never closes the fd until exit. Probes try `flock(LOCK_EX | LOCK_NB)`: `EAGAIN`/`EWOULDBLOCK` → alive, success → dead.

### Sweep

Runs on every `live` verb that touches sessions. Concurrent sweepers are race-safe: `O_EXCL` for `deadAt` creation, unlinks tolerate `ENOENT`. All reader and sweeper code paths tolerate the session directory itself disappearing mid-operation (`FileNotFoundError` on the dir → skip).

```
for each session in this .live/sessions/:
  if process.lock missing:       # session is in startup
      skip
  if process.lock NOT held AND no deadAt:
      create deadAt (O_EXCL)
  if process.lock NOT held AND now − mtime(deadAt) > ttlDays × 86400s:
      delete session
```

"NOT held" means the file exists and `flock(LOCK_EX | LOCK_NB)` on a fresh fd succeeds; close the probe fd immediately.

### `deadAt` marker

- **Empty file** = `consistent`. Recorder reached graceful exit.
- **`"inconsistent\n"`** = writer was killed mid-write or hit a disk error.

Graceful exit stamps `deadAt` directly. Sweepers compute the verdict by comparing complete-line count in `stream.<lastSegment>.log` against `os.path.getsize(lines.<lastSegment>.idx) // 16` (treat missing idx as 0 records — crashed mid-rotation). Equal → consistent. Any drift → inconsistent; stream-one-ahead is the expected crash shape, anything else violates the write-order invariant and is logged.

`deadAt`'s mtime is the TTL clock. Live sessions are never cleaned.

### Graceful exit

On normal child exit: write `meta.exitedAt` and `meta.exitCode`, atomically replace meta, create an empty `deadAt`, then close the lock fd. The `deadAt`-before-unlock order is required so no sweep can race in and stamp its own (possibly differing) verdict. `SIGTERM`/`SIGHUP`/`SIGINT` route to the same path.

`live run` then exits with the child's exit code: `os.WEXITSTATUS(status)` if the child exited normally, `128 + os.WTERMSIG(status)` if it died on a signal. `meta.exitCode` records the same value.

### `live rm -f` on a running session

1. Probe `process.lock` (open + `flock(LOCK_EX | LOCK_NB)`, then close the fd). If the probe acquired the lock, the recorder is gone (the pid in the file may have been recycled) — skip to step 5.
2. Read the pid from `process.lock` and `SIGTERM` the recorder.
3. Wait up to 5s for `flock` release, re-probing periodically (close each probe fd).
4. `SIGKILL` if still alive.
5. Unlink the session directory.

Without `-f`, `rm` prints an error and continues with the next selector.

## Configuration

`~/.live/config.json` is auto-created with defaults:

```json
{ "ttlDays": 7, "maxKb": 512, "segmentKb": 64, "heartbeatSec": 30 }
```

Any `.live/` may carry its own `config.json` to override fields. Per-field layering: per-`.live/` over home over compiled defaults. Partial files are valid.

Validation is a hand-rolled pass over the parsed JSON: `ttlDays >= 0`, `maxKb > 0`, `segmentKb > 0`, `heartbeatSec > 0`, all integers. Unknown keys ignored; out-of-range or wrong-typed fields fall back to the layer below.

- Malformed per-project config: log + ignore.
- Malformed home config: warn and fall back to defaults.

## Shell completion

`live <TAB>` offers verbs. `live run` consumes its own flags then defers to the wrapped command's completion. Selector verbs complete session names (`live cat <TAB>`, `live tail <TAB>`, `live rm <TAB>`).

- **bash**: `complete -F _live live`; `_command_offset` after `run`'s flags.
- **zsh**: `compdef _live live`; `_normal` for the `run` payload.
- **fish**: `__fish_use_subcommand` for verbs; `__fish_complete_subcommand` for `run` payload.

Install:

```sh
live completion bash > ~/.local/share/bash-completion/completions/live
live completion zsh  > "${fpath[1]}/_live"
live completion fish > ~/.config/fish/completions/live.fish
```

## Implementation

Python 3.14+. `pyproject.toml` with `hatchling`, `[project.scripts] live = "live.cli:main"`. PyPI: `astralarya-live`. Install via `pipx install astralarya-live` or `uv tool install astralarya-live`.

Zero runtime dependencies. PTY, flock, ioctl, signals, atomic rename, struct packing, JSON parsing, and UUIDv7 are all stdlib. `select.kqueue` covers Darwin; Linux/WSL gets inotify via an in-tree `ctypes` shim around `inotify_init1` / `inotify_add_watch` / `read`. No native build step.

## Defaults

| Thing        | Value                                                          |
| ------------ | -------------------------------------------------------------- |
| Scope        | walk up from cwd to nearest `.live/`; fallback `~/.live/`      |
| Capture      | PTY, merged stdout + stderr                                    |
| TTL          | 7 days from `deadAt` mtime, dead sessions only                 |
| Segment size | 64 KB rotation threshold; lines never split                    |
| Retention    | 512 KB total per session; oldest segments unlinked when over   |
| Index format | binary, 16-byte `struct.pack(">Qd", n, t)` in `lines.NNNN.idx` |
| Liveness     | held flock on `process.lock`                                   |
| Heartbeat    | active `lines.*.idx` mtime advanced every 30s (`heartbeatSec`) |
| Config       | `~/.live/config.json` plus optional per-`.live/` overrides     |
