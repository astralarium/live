---
name: live-cmd
description: Run long-lived commands `live` and inspect logs. Use for long running services like dev servers, databases, or watchers. Inspect user commands "running in live".
---

# live-cmd

`live` runs commands and provides tools to inspect logs in real-time.
Built for agents: reads use familiar interfaces like `cat` and `tail`.
Verbose reads emit a resume cursor to pick up where you left off, even across sessions.

No daemons; state lives in `~/.live`.

## Record a command

```bash
live run -n server npm start   # run under a PTY, record output
live run -dn server npm start  # detach: return immediately, print UUID
```

The command's stdout and stderr are merged into one log.
`live run -d` detaches the process so it survives shell exit.

## Find sessions

```bash
live ps         # active sessions in the current directory tree
live ps -a      # include exited sessions
live ps -ag     # all sessions, global scope
live ps --json  # NDJSON, one session per line
```

Select sessions by NAME (newest match) or UUID prefix.
Verbs scope to sessions started in the current directory and descendants;
add `-g` for global scope.

## Read output

```bash
live cat -v server
live head -vn 50 server  # first 50 lines
live tail -vn 50 server  # last 50 lines
live head -t 1m server   # lines at or before epoch T or (now - interval)
live tail -t 1m server   # lines after epoch T or (now - interval)
```

With `-v`, log content goes to stdout and verbose `live` metadata goes to stderr:

- trailer: printed with every verbose command
  `live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>`
- stop: session is done
  `live: exit-code=<code>` or `live: exit=inconsistent`
- hung: alive, but stalled
  `live: status=hung last-activity=<s>`
- tty closed: output detached but child is running
  `live: tty closed; no further output`
- gap: retention dropped data
  `live: dropped <j> lines + <k> bytes (from-line=<N>, first-line=<F>, from-byte=<B0>, first-byte=<B1>)`
- partial: partial line (eg. progress bars)
  `live: partial-line bytes=<k> age=<s>`

ANSI codes are stripped when stdout is not a TTY;
force with `--strip-ansi` or `--raw`.

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

Logs are bounded: past retention cap, old segments are dropped.
Old sessions are cleaned opportunistically
(default TTL 7 days, configurable in `~/.live/config.json`).
