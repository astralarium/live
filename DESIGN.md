# `live` — design

Stream long-lived command output to coding agents. `live run <cmd>` runs `<cmd>` under a PTY, mirrors output to the terminal, and records the bytes to disk under `~/.live/`. Agents read with `live cat`, `live tail`, or resumable `live tail -n +N`, piping to `grep`/`awk` as needed.

The recorder is the sole writer per session and holds an exclusive `flock` on `process.lock` for its lifetime — that lock IS the liveness signal. Read verbs hold no per-process state and piggyback lifecycle sweeps. No daemon, no broker, no persistent server.

Python 3.10+, POSIX-only (Linux, macOS, WSL). Zero runtime deps — PTY, flock, ioctl, signals, atomic rename, struct packing, JSON, UUIDv4, and the kqueue/inotify primitives that power `tail -f` are all stdlib. PyPI: `live-cmd`.

## CLI

| Verb                                                                               | Purpose                                                                                                                                               |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live run [-d] [-n NAME] [--geometry CxR] [--] <cmd…>`                             | Run `<cmd>` under a PTY; record. `-d` detaches (survives shell exit) and prints the session id. `--geometry` pins the PTY size (`COLSxROWS`; default: the terminal's size, else 80x24). NAME is `[A-Za-z0-9._-]` (no leading `-`); it errors if already running in an ancestor or descendant cwd — any scope that would see both — while siblings/disjoint dirs may share a name. Only in-scope conflicts hint `live stop`; an ancestor's run is out of scope from below. The conflict check and session creation hold a global name lock, so concurrent named runs can't race past it. |
| `live ls [-a] [-g] [--json] [SELECTOR]`                                            | List sessions in scope; `SELECTOR` filters by NAME or UUID-prefix.                                                                                    |
| `live cat [-v] [-g] [--strip-ansi\|--raw] <SELECTOR>`                              | Concatenate session.                                                                                                                                  |
| `live head [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                                | `-n N` first N lines (default 10; `-N` drops last N), `-c K` first K bytes (`-K` drops last K), `-t T` lines with idx `t <= T`.                       |
| `live tail [-f] [-v] [-g] [-n N\|-c K\|-t T] <SELECTOR>`                           | `-n N` last N lines (default 10; `+N` for `n >= N`), `-c K` last K bytes (`+K` for bytes past offset K), `-t T` lines with idx `t > T`; `-f` follows. |
| `live less [-g] [--strip-ansi\|--raw] <SELECTOR>`                                  | Page session in a less-style curses viewer; `F` follows new output. Falls back to `cat` when stdout isn't a TTY.                                      |
| `live stop [-g] [--all] <SELECTOR…>`                                               | Stop running sessions (SIGTERM the recorder; SIGKILL after 5s).                                                                                       |
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
live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>
```

`<N>` and `<B>` are resume cursors — plug straight into `tail -n +N` or `tail -c +B` to read what's been written since. Reset to `0` when `<uuid>` changes. `<T>` (active stream mtime, partial-bytes-aware since heartbeats only touch idx) is the alternate for `tail -t T`. `<B>` is a lifetime byte offset that survives segment rotation.

Possible preceding lines, in order: `dropped <k> lines (from-line=<N>, first-line=<F>)` / `dropped <k> bytes (from-byte=<B>, first-byte=<F>)` (gap), `from-line=<N> > next-line=<N>; check id` / `from-time=<T> > last-time=<T>; check id` / `from-byte=<B> > next-byte=<B>; check id` (cursor ahead), `partial-line bytes=<k> age=<s>`, `status=hung last-activity=<s>`, `exit=inconsistent`, `exit-code=<N>`. The last two can co-appear if the recorder wrote meta before a sweeper observed a torn recording.

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
      lines.NNNN.idx    # binary line index: 8-byte header (>Q segment start byte)
                        # then 24-byte records (>Qdq: n, t, line start byte), one per line
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
- **Signals.** `SIGWINCH` propagates window size (unless `--geometry` pinned it). `SIGTERM`/`SIGHUP` forward to the child; if the child hasn't exited 3s later, the recorder SIGKILLs its process group so a TERM-ignoring command can't outlive its session. `SIGINT` forwards only when stdin isn't a TTY (otherwise line discipline routes ^C directly). `live run` exits with the child's code, or `128 + signum` if signal-killed.
- **Detach.** `run -d` forks the recorder under `setsid` with fds on `/dev/null` — no controlling TTY, so shell exit can't reach it — and returns once the session dir + lock exist (the printed id is immediately visible to `ls`). The child PTY gets 80x24 unless `--geometry` says otherwise. `stop` SIGTERMs via the lock-file pid; the recorder's graceful-exit ordering still applies, and its kill escalation beats `stop`'s 5s SIGKILL deadline.
- **Config.** `~/.live/config.json` loads on every invocation. Partial files valid; unknown keys ignored; malformed fields fall back to defaults with a stderr warning.
