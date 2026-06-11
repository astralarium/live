---
name: live-cmd
description: Run long-lived commands (dev servers, builds, watchers) under `live` and stream their logs with cat/tail semantics. Use when starting a process whose output must be inspected later, when checking on a running or exited session, or when resuming log reads from a saved cursor without re-reading old output.
---

# live-cmd

`live` records command output to disk so it can be read with POSIX-style
verbs (`cat`, `head`, `tail`, `less`). It is built for agents: every read can
emit a resume cursor, so you never re-read old output and never miss new
output. No daemons; state lives in `~/.live`.

## Record a command

```bash
live run -n server npm start        # run under a PTY, record output
live run -n build -- make -j8       # use `--` if the command has flags
live run -d -n server npm start     # detach: return immediately, print UUID
```

The command's stdout and stderr are merged into one log. Large logs
auto-rotate (oldest segments dropped past the cap).

With `-d` the process survives shell exit; read its output later with
`cat`/`tail` and end it with `live stop`.

## Find sessions

```bash
live ls             # active sessions in the current directory tree
live ls -a          # include exited sessions
live ls -ag         # all sessions, global scope
live ls --json      # NDJSON, one session per line
```

Select sessions by NAME (newest match) or UUID prefix. All verbs scope to
sessions started in the current directory and descendants; add `-g` for
global scope.

## Read output

```bash
live cat -v server      # full log
live head -v -n 50 server
live tail -v -n 50 server
live tail -f server     # follow until exit
```

With `-v`, log content goes to stdout and `live` metadata goes to stderr:

- trailer: `live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>`
- stop: `live: exit-code=<code>` or `live: exit=inconsistent`
- hung: `live: status=hung last-activity=<s>` (alive, but stalled)
- gap (lines): `live: dropped <k> lines (from-line=<N>, first-line=<F>)`
- gap (bytes): `live: dropped <k> bytes (from-byte=<B>, first-byte=<F>)`
- partial: `live: partial-line bytes=<k> age=<s>`

ANSI codes are stripped when stdout is not a TTY; force with `--strip-ansi`
or `--raw`.

## Resume from a cursor

Save `next-line` (or `next-byte`) from the trailer, then poll for new data:

```bash
live tail -vn +42 server    # lines from line 42 onward
live tail -vc +250 server   # bytes from offset 250 onward
```

Each call prints a fresh trailer; feed `next-line`/`next-byte` into the next
poll. If the trailer's `id` changes, the name now points at a new session —
reset the cursor to 0. A `dropped` line means rotation outran the cursor;
continue from `first-line`/`first-byte`.

A session is done when stderr shows `exit-code=`. Time-based reads are also
available: `head -t <T>` (lines at or before epoch T), `tail -t <T>` (after T).

## Stop and clean up

```bash
live stop server                 # SIGTERM a running session
live stop --all                  # stop everything running in scope
live rm server                   # remove a session
live rm -f server                # SIGTERM a live run first
live rm --exited --older-than 1d
```

Old sessions are also cleaned opportunistically (default TTL 7 days,
configurable in `~/.live/config.json`).
