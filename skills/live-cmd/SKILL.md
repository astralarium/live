---
name: live-cmd
description: Run long-lived commands (dev servers, builds, watchers) under `live` and stream their logs with cat/tail semantics. Use when starting a process whose output must be inspected later, when checking on a running or exited session, or when resuming log reads from a saved cursor without re-reading old output.
---

# live-cmd

`live` records command output to disk so it can be read with
POSIX-style verbs (`cat`, `head`, `tail`, `less`).
It is built for agents: verbose reads emit a resume cursor,
so you never re-read old output and never miss new output.
No daemons; state lives in `~/.live`.

## Record a command

```bash
live run -n server npm start   # run under a PTY, record output
live run -dn server npm start  # detach: return immediately, print UUID
```

The command's stdout and stderr are merged into one log.
Logs are bounded: past the cap, old output is dropped.
Reads report gaps on stderr.

With `live run -d` the process survives shell exit.
Read output later with `cat`/`tail`. End it with `live stop`.

## Find sessions

```bash
live ps         # active sessions in the current directory tree
live ps -a      # include exited sessions
live ps -ag     # all sessions, global scope
live ps --json  # NDJSON, one session per line
```

Select sessions by NAME (newest match) or UUID prefix. All verbs scope to
sessions started in the current directory and descendants; add `-g` for
global scope.

## Read output

```bash
live cat -v server
live head -vn 50 server  # first 50 lines
live tail -vn 50 server  # last 50 lines
live head -t 1m server   # lines at or before epoch T or (now - interval)
live tail -t 1m server   # lines after epoch T or (now - interval)
```

With `-v`, log content goes to stdout and `live` metadata goes to stderr:

- trailer: printed with every verbose command
  `live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>`
- stop: session is done
  `live: exit-code=<code>`
  or
  `live: exit=inconsistent`
- hung: alive, but stalled
  `live: status=hung last-activity=<s>`
- tty closed: output detached but child is running
  `live: tty closed; no further output`
- gap: rotation dropped output (at most one per read)
  `live: dropped <j> lines + <k> bytes (from-line=<N>, first-line=<F>, from-byte=<B0>, first-byte=<B1>)`
- partial: partial line (eg. progress bars)
  `live: partial-line bytes=<k> age=<s>`

ANSI codes are stripped when stdout is not a TTY; force with `--strip-ansi`
or `--raw`.

## Resume from a cursor

Save `next-line` (or `next-byte`) from the trailer, then poll for new data:

```bash
live tail -vn +42 server   # lines from line 42 onward
live tail -vc +250 server  # bytes from position 250 (1-based)
```

Each call prints trailer data; feed `next-line`/`next-byte` into the next poll.
If trailer `id` changes, the name now points at a new session — reset cursors to 1.

## Stop and clean up

```bash
live stop server                  # SIGTERM a running session
live stop --all                   # stop everything running in scope
live rm server                    # remove a session
live rm -f server                 # stop and remove a session
live rm --exited --older-than 1d  # remove sessions that exited > 1 day ago
```

Old sessions are cleaned opportunistically
(default TTL 7 days, configurable in `~/.live/config.json`).
