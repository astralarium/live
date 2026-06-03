# `live` — design

Stream CLI command output to agents. `live run <cmd>` wraps a command under a PTY, mirrors output to the terminal, and records the bytes to disk in the nearest `.live/`.
Inspect command output with `live cat` and `live tail`, piping to shell tools like `grep`.

The recorder is the sole writer of session content. Read verbs are stateless and run lifecycle sweeps. No daemon, no broker, no persistent state.

Python 3.14+, POSIX-only (Linux, macOS, WSL).

## CLI

| Verb                                                                      | Purpose                                                                                                                                                                                              |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-n NAME] [--] <cmd…>`                                          | Wrap `<cmd>` under a PTY, mirror to stdout, record to disk.                                                                                                                                          |
| `live ls [-g] [-n NAME] [-a] [--json]`                                    | List sessions in scope. `-a` / `--all` includes exited; `--json` emits NDJSON with the full per-session field set.                                                                                   |
| `live cat [-g] [-v] <SELECTOR>`                                           | Concatenate all `stream.*.log` for the session. `-v` adds stderr metadata.                                                                                                                           |
| `live tail [-g] [-v] [-n LINES \| -c BYTES \| --since-line N] <SELECTOR>` | Tail. Unix `tail` flag conventions; `--since-line N` outputs lines after `N` for resumable polling and implies `-v`.                                                                                 |
| `live rm [-g] [-f] [--all-exited] <SELECTOR…>`                            | Delete sessions. `-f` SIGTERMs running recorders and ignores nonexistent. `--all-exited` removes every dead session in scope. Per-selector errors don't abort the batch; nonzero exit if any failed. |
| `live init`                                                               | Create `.live/`, `.live/sessions/`, and `.live/.gitignore` in cwd. Idempotent.                                                                                                                       |
| `live llms.txt`                                                           | Print a token-minimal agent guide for `live tail --since-line` polling.                                                                                                                              |
| `live completion <bash\|zsh\|fish>`                                       | Print the shell completion script.                                                                                                                                                                   |

`live`, `live -h`: usage. `live <verb> -h`: per-verb help. `live --version`.

### Scope

Read verbs walk up from cwd to the nearest `.live/`, then recursive `os.scandir` from its parent. If walk-up finds nothing, scope is `~/.live/`.

`-g` / `--global` (read verbs only) skips the walk-up and recurses from `~`.

`run` targets a single `.live/`: nearest one walking up from cwd, fallback `~/.live/` (auto-created on first use).

Walker rules: don't descend into `node_modules`, dotdirs other than `.live/`, or a found `.live/`; `is_dir(follow_symlinks=False)`; silently skip unreadable subtrees.

### Selectors

A selector is a single positional token, resolved like a git ref — names first, hash prefix as fallback:

1. **NAME** — any in-scope session with `meta.name == token`. For `cat` / `tail`, the most recent match wins. For `rm`, every match is selected.
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

Additional stderr lines may precede the trailer:

- Gap (`N + 1 < firstLine` because retention dropped lines, or `cat` reading a session whose oldest segment has been unlinked): `live: dropped <k> lines (since=<N>, first retained=<firstLine>)`. For `cat`, `<N>` is `0`.
- Cursor ahead (`tail --since-line` with `N > lastLine`, likely session swap): `live: since-line=<N> > at-line=<L>; check id`.
- Exited session (graceful): `live: exit-code=<N>`. Torn recordings (`deadAt = "inconsistent"`) emit `live: exit=inconsistent` instead. Omitted for running sessions.

Errors are always printed regardless of `-v`, with the same `live: ` prefix:

- Missing session: stderr `live: no such session: <selector>`, exit 2. No stdout, no trailer.

### `live tail --since-line`

Resumable polling for agents. Outputs lines with `n > N` to stdout. `--since-line` is mutually exclusive with `-n` / `-c`, and implies `-v` (see [Verbose output](#verbose-output)).

- Caught up (`N == lastLine`): empty stdout, trailer, exit 0.
- Cursor ahead (`N > lastLine`): see [Verbose output](#verbose-output).
- Gap (`N + 1 < firstLine`): see [Verbose output](#verbose-output); stdout starts from the oldest retained line. Exit 0.
- Exited session: drained like any live session — tail emits the remaining lines and the trailer. Lifecycle status (still running vs. exited) is observed via `live ls --json`, not `tail`.

### `live ls`

Lists sessions in scope, newest-first (UUIDv7 lex desc). Running only by default; `-a` / `--all` includes exited. `-n NAME` filters to that label.

Default output: human columns — id-prefix, status, name (if set), command. `--json` emits NDJSON, one object per session, with the full field set:

- `id`, `command`, `cwd`, `startedAt`
- `name?` — present iff started with `-n NAME`
- `status` — `"running"` | `"hung"` | `"exited"` | `"inconsistent"`. `"hung"` = flock held but `now − lastActivity > 3 × heartbeatSec`. `"inconsistent"` = torn recording (from `deadAt` content).
- `exitedAt?` — see [`meta.json`](#metajson) precedence
- `exitCode?` — present on graceful exit
- `path` — absolute session directory
- `firstSegment`, `lastSegment`
- `firstLine`, `lastLine`, `count`
- `lastActivity` — ms-since-epoch mtime of the active `lines.*.idx`

### `live llms.txt`

Prints a snippet for users to add to their agent docs. Positional args are used as session options in the output list. The literal payload:

```
This project uses `live`, a CLI streamer. The following sessions are available:
  * [FILL IN HERE]

List available sessions:
  live ls [-a] [--json]

Read output from a live session:
  live tail --since-line N <SELECTOR>
    stdout: lines with n>N
    stderr trailer: live: id=<uuid> at-line=<L>
    resume: next N = <L>; reset N=0 if <uuid> changes
    stop:   stderr has "live: exit-code=" or "live: exit=inconsistent"
    gap:    stderr "live: dropped <k> lines (since=<N>, first retained=<F>)"

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
  "startedAt": 1717200000000,
  "exitedAt": null,
  "exitCode": null
}
```

- `name` present only when started with `-n NAME`.
- Timestamps are integer milliseconds: `int(time.time() * 1000)`.

Writer-only. Written at session start and graceful exit. Atomic via `tempfile.NamedTemporaryFile` → `os.fsync` → `os.replace`.

Segment list and watermarks are derived from the filesystem:

- Segments: sort `stream.NNNN.log` / `lines.NNNN.idx` filenames numerically. `firstSegment` / `lastSegment` are the min/max.
- `firstLine` = first record's `n` in `lines.<firstSegment>.idx`.
- `lastLine` = unpack the trailing 16 bytes of `lines.<lastSegment>.idx`; if empty, walk back one segment.
- `count` = `lastLine − firstLine + 1`.

`exitedAt` precedence: `meta.exitedAt` (graceful, exact) → mtime of the active `lines.*.idx` (crash; bounded within `heartbeatSec`) → `mtime(deadAt)` (fallback).

### `lines.NNNN.idx`

Append-only binary, 16-byte records: `struct.pack(">QQ", n, t)` — uint64 BE line number, uint64 BE millisecond timestamp.

## Recording

Goal: `live <cmd>` is transparent — keystrokes, prompts, Ctrl-C, and resize reach `<cmd>` directly.

Child: `pty.fork()` → `os.execvp`. Parent selects on `[0, master_fd, wakeup_fd]`. If `os.isatty(0)`, stdin is put in raw mode (`tty.setraw(0)`, saved and restored on exit); otherwise raw mode is skipped and the PTY still works with redirected/piped stdin.

- `master_fd` → write to `sys.stdout.buffer`, append to `stream.NNNN.log`, update line index.
- stdin → write to `master_fd`. The PTY's line discipline routes ^C to the child's pgroup.
- `wakeup_fd` (`signal.set_wakeup_fd`) delivers signals: `SIGWINCH` propagates size via `TIOCGWINSZ` / `TIOCSWINSZ`; `SIGTERM` / `SIGHUP` forward to the child and graceful-exit. No `SIGINT` handler.

### Line indexing

- On the first byte of a new line, capture `t = int(time.time() * 1000)`.
- On `\n`, append `struct.pack(">QQ", n, t)` to `lines.NNNN.idx`.
- Trailing partial lines are not recorded until the newline arrives.

`n` is absolute across the session's lifetime. Retention deletes segments but never renumbers.

### Idle heartbeat

The recorder advances `lines.<lastSegment>.idx`'s mtime at least every `heartbeatSec`. The select loop uses that as its timeout; on idle wake-up, `os.utime` if no write touched the file within the interval.

flock detects process death; mtime staleness detects a hung process (wire `status: "hung"` when `now − mtime > 3 × heartbeatSec`).

### Write-order invariant

Each line: stream byte append, then index record append. **Prefix invariant**: index records are always a prefix of complete lines in stream. A crash leaves one extra complete line in stream with no index record, never the reverse.

If a `lines.*.idx` write raises `OSError` mid-session, the recorder kills the PTY and stamps `deadAt` with `inconsistent` before exiting nonzero.

## Segments and retention

Configurable per `.live/`:

- `segmentKb` (default 64) — rotate after a completed line carries the active segment past this.
- `maxKb` (default 512) — total retained `stream.*.log` bytes per session.

**Rotation.** When the active segment hits `segmentKb` at a line boundary, close it and open a new pair. Lines never split; an oversize line produces a fat segment and rotates after. Pure filesystem op — no meta write.

**Reader tolerance.** Readers may briefly see one of the new pair before the other (recorder creates `stream.NNNN+1.log` then `lines.NNNN+1.idx`). Treat `ENOENT` on the highest-numbered segment's files as empty and walk back one for `lastLine`. `ENOENT` on any lower segment is a real error.

**Retention.** After each rotation, sum `stream.*.log` bytes. While over `maxKb`, `os.unlink` the lowest-numbered pair.

**Reader race.** Between a `cursor` response and the agent opening the files, retention may unlink the lowest segment. `open()` returns `ENOENT`. The next `cursor` returns a fresh list with `gap: true` if tracked lines were dropped.

## Liveness and cleanup

**Liveness** = recorder holds an exclusive `fcntl.flock` on `process.lock`. Recorder opens with `O_WRONLY | O_CREAT`, takes `flock(LOCK_EX | LOCK_NB)`, writes its pid as decimal ASCII, never closes the fd until exit. Probes try `flock(LOCK_EX | LOCK_NB)`: `EAGAIN`/`EWOULDBLOCK` → alive, success → dead.

### Sweep

Runs on every `live` verb that touches sessions. Concurrent sweepers are race-safe: `O_EXCL` for `deadAt` creation, unlinks tolerate `ENOENT`.

```
for each session in this .live/sessions/:
  if process.lock NOT held AND no deadAt:
      create deadAt (O_EXCL)
  if process.lock NOT held AND now − mtime(deadAt) > ttlDays × 86400s:
      delete session
```

### `deadAt` marker

- **Empty file** = `consistent`. Recorder reached graceful exit.
- **`"inconsistent\n"`** = writer was killed mid-write or hit a disk error.

Graceful exit stamps `deadAt` directly. Sweepers compute the verdict by comparing complete-line count in `stream.<lastSegment>.log` against `os.path.getsize(lines.<lastSegment>.idx) // 16`. Equal → consistent; stream one ahead → inconsistent.

`deadAt`'s mtime is the TTL clock. Live sessions are never cleaned.

### Graceful exit

On normal child exit: write `meta.exitedAt` and `meta.exitCode`, atomically replace meta, create an empty `deadAt`, close the lock fd. `SIGTERM`/`SIGHUP`/`SIGINT` route to the same path.

`live run` then exits with the child's exit code: `os.WEXITSTATUS(status)` if the child exited normally, `128 + os.WTERMSIG(status)` if it died on a signal. `meta.exitCode` records the same value.

### `live rm -f` on a running session

1. Probe `process.lock` with `flock(LOCK_EX | LOCK_NB)`. If the lock is free, skip to step 5 — the recorder is already gone and the pid in the file may have been recycled.
2. Read the pid from `process.lock` and `SIGTERM` the recorder.
3. Wait up to 5s for `flock` release.
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

`live <TAB>` offers verbs. `live run` consumes its own flags then defers to the wrapped command's completion. Selector verbs complete session names (`live cat --name <TAB>`).

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

Zero runtime dependencies. PTY, flock, ioctl, signals, atomic rename, struct packing, JSON parsing, and UUIDv7 are all stdlib. No native build step.

## Defaults

| Thing               | Value                                                                                                                                  |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Read scope          | walk up from cwd to nearest `.live/`, recursive `os.scandir` from its parent; fallback `~/.live/`; `-g` (reads only) recurses from `~` |
| Write scope (`run`) | nearest `.live/` walking up from cwd; fallback `~/.live/`                                                                              |
| Capture             | PTY, merged stdout + stderr                                                                                                            |
| TTL                 | 7 days from `deadAt` mtime, dead sessions only                                                                                         |
| Segment size        | 64 KB rotation threshold; lines never split                                                                                            |
| Retention           | 512 KB total per session; oldest segments unlinked when over                                                                           |
| Index format        | binary, 16-byte `struct.pack(">QQ", n, t)` in `lines.NNNN.idx`                                                                         |
| Liveness            | held flock on `process.lock`                                                                                                           |
| Heartbeat           | active `lines.*.idx` mtime advanced every 30s (`heartbeatSec`)                                                                         |
| Config              | `~/.live/config.json` plus optional per-`.live/` overrides                                                                             |
