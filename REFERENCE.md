# `live` — command reference

- [`live`](#live)
- [`live run`](#live-run)
- [`live ls`](#live-ls)
- [`live cat`](#live-cat)
- [`live head`](#live-head)
- [`live tail`](#live-tail)
- [`live less`](#live-less)
- [`live stop`](#live-stop)
- [`live rm`](#live-rm)
- [`live completion`](#live-completion)
- [`live completion-script`](#live-completion-script)
- [`live update-shell`](#live-update-shell)
- [`live llms.txt`](#live-llmstxt)

## `live`

```
usage: live [-h] [--version] <verb> ...

Live stream command line output.

positional arguments:
  <verb>
    run                Run <cmd> under a PTY; record.
    ls                 List sessions.
    cat                Concatenate session.
    head               Head session.
    tail               Tail session.
    less               Page session.
    stop               Stop running sessions.
    rm                 Delete sessions.
    completion         Print completion candidates.
    completion-script  Print shell completion script.
    update-shell       Install shell completions.
    llms.txt           Print agent instructions.

options:
  -h, --help           show this help message and exit
  --version            show program's version number and exit
```

## `live run`

```
usage: live run [-h] [-n NAME] [-d] [-C PATH] [--geometry COLSxROWS] cmd ...

Run a command under a PTY and record its output.

positional arguments:
  cmd                   Command to run; `--` wraps subsequent arguments.

options:
  -h, --help            show this help message and exit
  -n, --name NAME       Session name (letters, digits, '.', '_', '-').
  -d, --detach          Detach: run in the background, print session id.
  -C, --cwd PATH        Run <cmd> in PATH; scope session.
  --geometry COLSxROWS  PTY size (default: the terminal's size, else 80x24).
```

## `live ls`

```
usage: live ls [-h] [-a] [-g | -C PATH] [--json] [selector]

List recorded sessions.

positional arguments:
  selector        NAME or UUID-prefix filter.

options:
  -h, --help      show this help message and exit
  -a, --all       Include exited.
  -g, --global    Global scope.
  -C, --cwd PATH  Directory scope (default: current directory).
  --json          Emit NDJSON.
```

## `live cat`

```
usage: live cat [-h] [-v] [-g | -C PATH] [--strip-ansi | --raw] selector

Display a session's full output.

positional arguments:
  selector        NAME or UUID-prefix.

options:
  -h, --help      show this help message and exit
  -v, --verbose   Verbose output.
  -g, --global    Global scope.
  -C, --cwd PATH  Directory scope (default: current directory).
  --strip-ansi    Strip ANSI.
  --raw           Keep ANSI.
```

## `live head`

```
usage: live head [-h] [-v] [-g | -C PATH] [--strip-ansi | --raw] [-n LINES | -c BYTES | -t TIME] selector

Display the first part of a session.

positional arguments:
  selector           NAME or UUID-prefix.

options:
  -h, --help         show this help message and exit
  -v, --verbose      Verbose output.
  -g, --global       Global scope.
  -C, --cwd PATH     Directory scope (default: current directory).
  --strip-ansi       Strip ANSI.
  --raw              Keep ANSI.
  -n, --lines LINES  First N lines (default 10); -N drops last N.
  -c, --bytes BYTES  First K bytes; -K drops last K.
  -t, --time TIME    Lines with idx t <= T: epoch, duration (30m), or ISO datetime.
```

## `live tail`

```
usage: live tail [-h] [-f] [-v] [-g | -C PATH] [--strip-ansi | --raw] [-n LINES | -c BYTES | -t TIME] selector

Display the last part of a session.

positional arguments:
  selector           NAME or UUID-prefix.

options:
  -h, --help         show this help message and exit
  -f, --follow       Follow until exit.
  -v, --verbose      Verbose output.
  -g, --global       Global scope.
  -C, --cwd PATH     Directory scope (default: current directory).
  --strip-ansi       Strip ANSI.
  --raw              Keep ANSI.
  -n, --lines LINES  Last N lines (default 10); +N for lines n >= N.
  -c, --bytes BYTES  Last K bytes; +K for bytes from position K.
  -t, --time TIME    Lines with idx t > T: epoch, duration (30m), or ISO datetime.
```

## `live less`

```
usage: live less [-h] [-g | -C PATH] [--strip-ansi | --raw] selector

Page session interactively.

positional arguments:
  selector        NAME or UUID-prefix.

options:
  -h, --help      show this help message and exit
  -g, --global    Global scope.
  -C, --cwd PATH  Directory scope (default: current directory).
  --strip-ansi    Strip ANSI.
  --raw           Keep ANSI.
```

## `live stop`

```
usage: live stop [-h] [-g | -C PATH] [--all] [selectors ...]

Stop running sessions.

positional arguments:
  selectors       NAME(s) or UUID-prefix(es).

options:
  -h, --help      show this help message and exit
  -g, --global    Global scope.
  -C, --cwd PATH  Directory scope (default: current directory).
  --all           Stop all running sessions.
```

## `live rm`

```
usage: live rm [-h] [-f] [-g | -C PATH] [--all] [--exited] [--untitled] [--older-than AGE] [selectors ...]

Remove recorded sessions.

positional arguments:
  selectors         NAME(s) or UUID-prefix(es).

options:
  -h, --help        show this help message and exit
  -f, --force       SIGTERM live runs; ignore missing.
  -g, --global      Global scope.
  -C, --cwd PATH    Directory scope (default: current directory).
  --all             Delete all sessions.
  --exited          Delete exited sessions.
  --untitled        Delete unnamed sessions.
  --older-than AGE  Delete sessions exited before AGE: duration (7d, 12h, 30m, 60s) or ISO datetime.
```

## `live completion`

```
usage: live completion [-h] <what> ...

Print completion candidates, one per line (plumbing for the shell completion scripts).

positional arguments:
  <what>
    selectors  Session names and ids.
    cwds       Session working directories.

options:
  -h, --help   show this help message and exit
```

## `live completion-script`

```
usage: live completion-script [-h] {bash,zsh,fish}

Print shell completion script.

positional arguments:
  {bash,zsh,fish}  Target shell.

options:
  -h, --help       show this help message and exit
```

## `live update-shell`

```
usage: live update-shell [-h] [{bash,zsh,fish}]

Install shell completions.

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

### Agent guide

```
This project uses `live`, a CLI streamer.
See live-cmd skill for detailed usage.

Run detached (survives shell exit; prints session UUID):
  live run -dn NAME -- <cmd>

Stop a running session:
  live stop <SELECTOR>

List sessions:
  live ls [-a] [--json] [<SELECTOR>]

<SELECTOR>: UUID prefix or NAME (newest match)

Read output:
  live cat -v <SELECTOR>
  live head -v <SELECTOR>

stdout: merged stdout+stderr logs

stderr: `live` verbose output (-v):
- trailer:
  "live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>"
- stop: session is done
  "live: exit-code=<code>" or "live: exit=inconsistent"
- hung: alive, but stalled
  "live: status=hung last-activity=<s>"
- tty closed: output detached but child is running
  "live: tty closed; no further output"
- gap: rotation dropped data
  "live: dropped <j> lines + <k> bytes (from-line=<N>, first-line=<F>, from-byte=<B0>, first-byte=<B1>)"
- partial: partial line (eg. progress bar)
  "live: partial-line bytes=<k> age=<s>"

Check for new data:
  live tail -vn +<N> <SELECTOR>  # by line
  live tail -vc +<B> <SELECTOR>  # by byte

Reset cursor to 1 if <uuid> changes (new session)
```

---

_Autogenerated by `scripts/gen_docs.py`. Do not edit by hand._
