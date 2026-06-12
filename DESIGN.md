# `live` — design

Stream long-lived command output to coding agents. `live run <cmd>` runs `<cmd>` under a PTY, mirrors output to the terminal, and records the bytes to disk under `~/.live/`. Agents read with `live cat`, `live tail`, or resumable `live tail -n +N`, piping to `grep`/`awk` as needed.

The recorder is the sole writer per session and holds an exclusive `flock` on `process.lock` for its lifetime — that lock IS the liveness signal. Read verbs hold no per-process state and piggyback lifecycle sweeps. No daemon, no broker, no persistent server.

Python 3.10+, POSIX-only (Linux, macOS, WSL). Zero runtime deps — PTY, flock, ioctl, signals, atomic rename, struct packing, JSON, UUIDv4, and the kqueue/inotify primitives that power `tail -f` are all stdlib. PyPI: `live-cmd`.

## CLI

| Verb                                                                               | Purpose                                                                                                                                               |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-d] [-n NAME] [--geometry CxR] [--] <cmd…>`                             | Run `<cmd>` under a PTY; record. `-d` detaches (survives shell exit) and prints the session id. `--geometry` pins the PTY size (`COLSxROWS`; default: the terminal's size, else 80x24). |
| `live ls [-a] [-g] [--json] [SELECTOR]`                                            | List sessions in scope; `SELECTOR` filters by NAME or UUID-prefix.                                                                                    |
| `live cat [-v] [-g] [--strip-ansi\|--raw] <SELECTOR>`                              | Concatenate session.                                                                                                                                  |
| `live head [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                                | `-n N` first N lines (default 10; `-N` drops last N), `-c K` first K bytes (`-K` drops last K), `-t T` lines with idx `t <= T` (T: epoch seconds, duration like `30m` for now − 30m, or ISO datetime). |
| `live tail [-f] [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                           | `-n N` last N lines (default 10; `+N` for `n >= N`), `-c K` last K bytes (`+K` for bytes from 1-based position K, GNU), `-t T` lines with idx `t > T` (same T forms as `head`); `-f` follows. |
| `live less [-g] [--strip-ansi\|--raw] <SELECTOR>`                                  | Page session in a less-style curses viewer; `F` follows new output. Falls back to `cat` when stdout isn't a TTY.                                      |
| `live stop [-g] [--all] <SELECTOR…>`                                               | Stop running sessions (SIGTERM the recorder; SIGKILL after 5s).                                                                                       |
| `live rm [-f] [-g] [--all] [--exited] [--untitled] [--older-than AGE] <SELECTOR…>` | Delete sessions matching `--all` or `<SELECTOR…>`, narrowed by `--exited`, `--untitled`, and `--older-than` (intersection). `--untitled` implies `--exited`; `--exited` implies `--all` when no selector is given. `-f` SIGTERMs live runs. |
| `live llms.txt`                                                                    | Print agent guide.                                                                                                                                    |
| `live completion <selectors\|cwds>`                                                | Print completion candidates, one per line (plumbing for the completion scripts).                                                                      |
| `live completion-script <bash\|zsh\|fish>`                                         | Print shell completion script.                                                                                                                        |
| `live update-shell [SHELL]`                                                        | Install completion for `$SHELL` (or override).                                                                                                        |

`live <verb> -h` for full flag details. Scope defaults to cwd-and-below; `-C PATH` re-roots it (for `run`: also the child's working directory), `-g` lifts it. ANSI: default strips when stdout isn't a TTY; `--strip-ansi` / `--raw` override.

An unterminated tail (open line) counts as the line after the last indexed one. Reads emit it only when that line falls inside the requested range: it occupies the newest slot for `tail -n N` and `head -n -K`, rides along with byte and time ranges that reach the stream end, and is never emitted when a cursor sits past it (GNU fragment semantics).

NAME is `[A-Za-z0-9._-]` (no leading `-`). `run -n` errors if NAME is already running in an ancestor or descendant cwd — any scope that would see both — while siblings/disjoint dirs may share a name. Only in-scope conflicts hint `live stop`; an ancestor's run is out of scope from below. The conflict check and session creation hold a global name lock, so concurrent named runs can't race past it. Acquisition is bounded: a waiter prints a notice naming the holder's pid after 1s and errors out at 5s. The detached recorder closes its inherited lock fd at fork, so a CLI dying mid-handshake can't strand the lock for the session's lifetime.

Exit codes: `0` success; `1` runtime error (missing session, ambiguous selector, not running, stop/rm failure); `2` usage error (bad flag or malformed argument). `run` exits with the child's code.

## Selectors

A single positional token, resolved like a git ref:

1. **NAME** match wins. `cat`/`head`/`tail` pick the most recent; `rm` operates on all matches.
2. **UUID prefix** fallthrough. Unique match required; ambiguous → error.

"Most recent" = descending `meta.startedAt`. Use `--` to pass a token starting with `-`.

## Verbose output

With `-v`, stderr carries metadata; without it, stderr is silent on success. Errors are always printed. The trailing line of any verbose read is:

```
live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>
```

`<N>` and `<B>` are resume cursors — plug straight into `tail -n +N` or `tail -c +B` to read what's been written since. Reset to `1` when `<uuid>` changes. `<T>` (active stream mtime, partial-bytes-aware since heartbeats only touch idx) is the alternate for `tail -t T`. `<B>` is a 1-based lifetime byte position (like all `from-byte`/`first-byte` values) and survives segment rotation.

Possible preceding lines, in order:

- `dropped <j> lines + <k> bytes (from-line=<N>, first-line=<F>, from-byte=<B0>, first-byte=<B1>)` — gap; at most one per read. Either clause may drop out with its key pair; the byte clause spans `[from-byte, first-byte)` — for line reads, the missing beginning of the first emitted line.
- `from-line=<N> > next-line=<N>; check id` (or the `from-time` / `from-byte` analogues) — cursor ahead of the stream.
- `partial-line bytes=<k> age=<s>` — unterminated tail (e.g. a progress bar).
- `status=hung last-activity=<s>` — alive but stalled.
- `exit=inconsistent`, `exit-code=<N>` — session is done. Both can appear if the recorder wrote meta before a sweeper observed a torn recording.

## On-disk layout

```
~/.live/
  config.json
  state.json          # sweep throttle stamp
  name.lock           # serializes named-run conflict check + creation
  sessions/
    <uuid>/
      meta.json         # session metadata; writer-only, replaced atomically
      process.lock      # held by the recorder for its lifetime — liveness signal
      deadAt            # post-mortem marker; mtime = TTL clock, content = verdict
      stream.NNNN.log   # raw PTY bytes
      lines.NNNN.idx    # binary line index: 16-byte header (>QQ segment start byte,
                        # start byte of the line open at that point) then 24-byte
                        # records (>QdQ: n, t, line start byte), one per line
```

The recorder appends to the highest-numbered pair; frozen segments are immutable until retention unlinks them. Session IDs are UUIDv4; chronological order comes from `meta.startedAt`.

Scope is a filter on `meta.cwd` (symlinks resolved).

## Invariants

- **Single writer, lock = liveness.** Recorder holds the `flock` for its lifetime. Probe with non-blocking `LOCK_EX`: success = recorder is gone.
- **Prefix invariant.** Stream is always one complete line ahead of, or equal to, the index — never the inverse. Crash leaves an unindexed complete line; sweepers stamp it `inconsistent`.
- **Hard cap.** Closed segments are exactly `segmentKb` — rotation lands mid-line, so a line may span segments and readers locate lines by idx byte offsets, never by per-segment newline counting. Retention runs on every rotation and keeps retained bytes ≤ `maxKb` + one segment, unconditionally: a line wider than the cap is head-truncated rather than retained whole, and output with no newlines at all is bounded the same way.
- **Absolute line numbers.** `n` is monotonic across the session's lifetime. Retention deletes but never renumbers; cursors past the oldest retained line get a `dropped` notice.
- **Heartbeat.** Recorder advances the active idx mtime every `heartbeatSec`. Staleness past `3 × heartbeatSec` while the lock is held = `hung`.
- **Opportunistic sweep.** Every verb invocation triggers a sweep — throttled to once an hour via `state.json` — that stamps dead-but-unmarked sessions (exclusive create of `deadAt`) and deletes those past `ttlDays`. Negative `ttlDays` disables the delete pass. Races are safe.
- **Graceful exit ordering.** `meta.json` → `deadAt` → unlock, in that order, so no sweeper races in with a wrong verdict.
- **Signals.** `SIGWINCH` propagates window size (unless `--geometry` pinned it). `SIGTERM`/`SIGHUP` forward to the child; if the child hasn't exited 3s later, the recorder SIGKILLs its process group so a TERM-ignoring command can't outlive its session. `SIGINT` forwards only when stdin isn't a TTY (otherwise line discipline routes ^C directly). `live run` exits with the child's code, or `128 + signum` if signal-killed.
- **TTY EOF with a live child.** A child can close its terminal and keep running (e.g. a server told to log to files): no output can ever arrive again, but the process is healthy. The recorder stamps `ttyClosedAt` in meta, restores the user's terminal (so `^C` forwards-and-escalates instead of wedging), notes it on stderr in the foreground, and keeps heartbeating through a non-blocking reap loop — `ls` shows `Running … (tty closed)`, never a false `hung`. `tail -f` drains and exits on the marker. Platform caveat: Linux masters raise EIO when the last slave fd closes; BSD/macOS masters only EOF on session-leader exit, so there this state stays a plain `running` (heartbeats genuinely continue). On exit, live survivors in the child's process group (e.g. `cmd &` from a wrapper shell) mark the session `detached` — `Exited … [detached]`, both platforms — best-effort: a daemon that re-`setsid`s escapes detection.
- **Detach.** `run -d` forks the recorder under `setsid` with fds on `/dev/null` — no controlling TTY, so shell exit can't reach it — and returns once the session dir + lock exist (the printed id is immediately visible to `ls`). The child PTY gets 80x24 unless `--geometry` says otherwise. `stop` SIGTERMs via the lock-file pid; the recorder's graceful-exit ordering still applies, and its kill escalation beats `stop`'s 5s SIGKILL deadline.
- **Config.** `~/.live/config.json` loads on every invocation; created with defaults if missing. Partial files valid (missing fields use defaults); unknown keys ignored. A malformed file or invalid known field is a hard error: every verb exits 1 naming the offending field, what was expected, and a hint to fix it or delete the file to regenerate defaults. Only `live` with no verb and `-h`/`--version` work with a broken config.
