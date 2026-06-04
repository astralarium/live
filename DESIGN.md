# `live` — design

Stream long-lived command output to coding agents. `live run <cmd>` runs `<cmd>` under a PTY, mirrors output to the terminal, and records the bytes to disk under `~/.live/`. Agents read with `live cat`, `live tail`, or resumable `live tail -n +N`, piping to `grep`/`awk` as needed.

The recorder is the sole writer per session and holds an exclusive `flock` on `process.lock` for its lifetime — that lock IS the liveness signal. Read verbs hold no per-process state and piggyback lifecycle sweeps. No daemon, no broker, no persistent server.

Python 3.10+, POSIX-only (Linux, macOS, WSL). Zero runtime deps — PTY, flock, ioctl, signals, atomic rename, struct packing, JSON, UUIDv4, and the kqueue/inotify primitives that power `tail -f` are all stdlib. PyPI: `live-cmd`.

## CLI

| Verb                                                                               | Purpose                                                                                                                                               |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-n NAME] [--] <cmd…>`                                                   | Run `<cmd>` under a PTY; record.                                                                                                                      |
| `live ls [-a] [-g] [--json] [SELECTOR]`                                            | List sessions in scope; `SELECTOR` filters by NAME or UUID-prefix.                                                                                    |
| `live cat [-v] [-g] [--strip-ansi\|--raw] <SELECTOR>`                              | Concatenate session.                                                                                                                                  |
| `live head [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                                | `-n N` first N lines (default 10; `-N` drops last N), `-c K` first K bytes (`-K` drops last K), `-t T` lines with idx `t <= T`.                       |
| `live tail [-f] [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                           | `-n N` last N lines (default 10; `+N` for `n >= N`), `-c K` last K bytes (`+K` for bytes past offset K), `-t T` lines with idx `t > T`; `-f` follows. |
| `live rm [-f] [-g] [--all] [--exited] [--untitled] [--older-than AGE] <SELECTOR…>` | Delete sessions matching `--all` or `<SELECTOR…>`, narrowed by `--exited`, `--untitled`, and `--older-than` (intersection). `--untitled` implies `--exited`; `--exited` implies `--all` when no selector is given. `-f` SIGTERMs live runs. |
| `live llms.txt`                                                                    | Print agent guide.                                                                                                                                    |
| `live completion <bash\|zsh\|fish>`                                                | Print shell completion.                                                                                                                               |
| `live update-shell [SHELL]`                                                        | Install completion for `$SHELL` (or override).                                                                                                        |

`live <verb> -h` for full flag details. `-g` widens scope from cwd-and-below to all sessions. ANSI: default strips when stdout isn't a TTY; `--strip-ansi` / `--raw` override.

Exit codes: `0` success; `1` runtime error; `2` usage error (bad flag, missing session, ambiguous selector).

## Selectors

A single positional token, resolved like a git ref:

1. **NAME** match wins. `cat`/`head`/`tail` pick the most recent; `rm` operates on all matches.
2. **UUID prefix** fallthrough. Unique match required; ambiguous → error.

"Most recent" = descending `meta.startedAt`. Use `--` to pass a token starting with `-`.

## Verbose output

With `-v`, stderr carries metadata; without it, stderr is silent on success. Errors are always printed. The trailing line of any verbose read is:

```
live: id=<uuid> at-line=<L> at-time=<T> at-byte=<B>
```

Agents using `tail -vn +N` pass `<L>+1` as the next cursor and reset to `0` when `<uuid>` changes. `<T>` (active stream mtime, partial-bytes-aware since heartbeats only touch idx) and `<B>` (cumulative byte cursor) are alternates for `tail -t T` / `tail -c +K`.

Possible preceding lines, in order: `dropped <k> lines …` (gap), `since=<N> > at-line=<L>; check id` / `time=<T> > at-time=<T>; check id` / `bytes=<B> > at-byte=<B>; check id` (cursor ahead), `partial-line bytes=<k> age=<s>`, `status=hung last-activity=<s>`, `exit=inconsistent`, `exit-code=<N>`. The last two can co-appear if the recorder wrote meta before a sweeper observed a torn recording.

## On-disk layout

```
~/.live/
  config.json
  sessions/
    <uuid>/
      meta.json         # session metadata; writer-only, replaced atomically
      process.lock      # held by the recorder for its lifetime — liveness signal
      deadAt            # post-mortem marker; mtime = TTL clock, content = verdict
      stream.NNNN.log   # raw PTY bytes
      lines.NNNN.idx    # binary line index: struct.pack(">Qd", n, t), one per line
```

The recorder appends to the highest-numbered pair; frozen segments are immutable until retention unlinks them. Session IDs are UUIDv4; chronological order comes from `meta.startedAt`.

Scope is a filter on `meta.cwd`: read verbs default to cwd-or-descendant (symlinks resolved); `-g` widens.

## Invariants

- **Single writer, lock = liveness.** Recorder holds the `flock` for its lifetime. Probe with non-blocking `LOCK_EX`: success = recorder is gone.
- **Prefix invariant.** Stream is always one complete line ahead of, or equal to, the index — never the inverse. Crash leaves an unindexed complete line; sweepers stamp it `inconsistent`.
- **Absolute line numbers.** `n` is monotonic across the session's lifetime. Retention deletes but never renumbers; cursors past the oldest retained line get a `dropped` notice.
- **Heartbeat.** Recorder advances the active idx mtime every `heartbeatSec`. Staleness past `3 × heartbeatSec` while the lock is held = `hung`.
- **Sweep on every read.** Each verb that touches sessions stamps dead-but-unmarked ones (exclusive create of `deadAt`) and deletes those past `ttlDays`. Negative `ttlDays` disables the delete pass. Races are safe.
- **Graceful exit ordering.** `meta.json` → `deadAt` → unlock, in that order, so no sweeper races in with a wrong verdict.
- **Signals.** `SIGWINCH` propagates window size. `SIGTERM`/`SIGHUP` forward to the child. `SIGINT` forwards only when stdin isn't a TTY (otherwise line discipline routes ^C directly). `live run` exits with the child's code, or `128 + signum` if signal-killed.
- **Config.** `~/.live/config.json` loads on every invocation. Partial files valid; unknown keys ignored; malformed fields fall back to defaults with a stderr warning.
