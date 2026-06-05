# `live` — command reference

- [`live`](#live)
- [`live run`](#live-run)
- [`live ls`](#live-ls)
- [`live cat`](#live-cat)
- [`live head`](#live-head)
- [`live tail`](#live-tail)
- [`live rm`](#live-rm)
- [`live completion`](#live-completion)
- [`live update-shell`](#live-updateshell)
- [`live llms.txt`](#live-llmstxt)

## `live`

```
usage: live [-h] [--version] <verb> ...

Live stream command line output. Inspect long processes from agent workflows.

positional arguments:
  <verb>
    run           Run <cmd> under a PTY; record.
    ls            List sessions in scope.
    cat           Concatenate session.
    head          Head session.
    tail          Tail session.
    rm            Delete sessions.
    completion    Print shell completion script.
    update-shell  Install completion for the current shell.
    llms.txt      Print agent instructions.

options:
  -h, --help      show this help message and exit
  --version       show program's version number and exit
```

## `live run`

```
usage: live run [-h] [-n NAME] cmd ...

Run a command under a PTY and record its output.

positional arguments:
  cmd              Command to run; `--` for flag-starting commands.

options:
  -h, --help       show this help message and exit
  -n, --name NAME  Session name.
```

## `live ls`

```
usage: live ls [-h] [-a] [-g] [--json] [selector]

List recorded sessions.

positional arguments:
  selector      NAME or UUID-prefix filter.

options:
  -h, --help    show this help message and exit
  -a, --all     Include exited.
  -g, --global  Global scope.
  --json        Emit NDJSON.
```

## `live cat`

```
usage: live cat [-h] [-v] [-g] [--strip-ansi | --raw] selector

Display a session's full output.

positional arguments:
  selector       NAME or UUID-prefix.

options:
  -h, --help     show this help message and exit
  -v, --verbose  Verbose output.
  -g, --global   Global scope.
  --strip-ansi   Strip ANSI.
  --raw          Keep ANSI.
```

## `live head`

```
usage: live head [-h] [-v] [-g] [--strip-ansi | --raw] [-n LINES | -c BYTES | -t TIME] selector

Display the first part of a session.

positional arguments:
  selector           NAME or UUID-prefix.

options:
  -h, --help         show this help message and exit
  -v, --verbose      Verbose output.
  -g, --global       Global scope.
  --strip-ansi       Strip ANSI.
  --raw              Keep ANSI.
  -n, --lines LINES  First N lines (default 10); -N drops last N.
  -c, --bytes BYTES  First K bytes; -K drops last K.
  -t, --time TIME    Lines with idx t <= T (epoch).
```

## `live tail`

```
usage: live tail [-h] [-f] [-v] [-g] [--strip-ansi | --raw] [-n LINES | -c BYTES | -t TIME] selector

Display the last part of a session.

positional arguments:
  selector           NAME or UUID-prefix.

options:
  -h, --help         show this help message and exit
  -f, --follow       Follow until exit.
  -v, --verbose      Verbose output.
  -g, --global       Global scope.
  --strip-ansi       Strip ANSI.
  --raw              Keep ANSI.
  -n, --lines LINES  Last N lines (default 10); +N for lines n >= N.
  -c, --bytes BYTES  Last K bytes; +K for bytes after offset K.
  -t, --time TIME    Lines with idx t > T (epoch).
```

## `live rm`

```
usage: live rm [-h] [-f] [-g] [--all] [--exited] [--untitled] [--older-than AGE] [selectors ...]

Remove recorded sessions.

positional arguments:
  selectors         NAME(s) or UUID-prefix(es).

options:
  -h, --help        show this help message and exit
  -f, --force       SIGTERM live runs; ignore missing.
  -g, --global      Global scope.
  --all             Delete all sessions in scope.
  --exited          Delete exited sessions.
  --untitled        Delete unnamed sessions.
  --older-than AGE  Delete sessions exited before AGE: duration (7d, 12h, 30m, 60s) or ISO datetime.
```

## `live completion`

```
usage: live completion [-h] {bash,zsh,fish}

Print a shell completion script.

positional arguments:
  {bash,zsh,fish}  Target shell.

options:
  -h, --help       show this help message and exit
```

## `live update-shell`

```
usage: live update-shell [-h] [{bash,zsh,fish}]

Install shell completion.

positional arguments:
  {bash,zsh,fish}  Target shell (default: $SHELL).

options:
  -h, --help       show this help message and exit
```

## `live llms.txt`

```
usage: live llms.txt [-h]

Print agent instructions.

options:
  -h, --help  show this help message and exit
```

### Agent instructions

```
This project uses `live`, a CLI streamer.

List available sessions:
  live ls [-a] [--json] [<SELECTOR>]

<SELECTOR>: UUID prefix or NAME (newest match)

Read output from a session:
  live cat -v <SELECTOR>
  live head -v <SELECTOR>

stdout: command stdout+stderr (merged)

stderr: live verbose output (-v):
  trailer: "live: id=<uuid> at-line=<L> at-time=<T> at-byte=<B>"
  stop: "live: exit-code=" or "live: exit=inconsistent"
  hung: "live: status=hung last-activity=<s>" (alive, but stalled)
  gap: "live: dropped <k> lines (since=<N>, first retained=<F>)"
  partial: "live: partial-line bytes=<k> age=<s>"

Check for new data from a session:
  live tail -vn +<N> <SELECTOR>

set +<N> = <L>+1 from last read
reset <N>=0 if <uuid> changes (new session)
```

---

_Autogenerated by `scripts/gen_docs.py`. Do not edit by hand._
