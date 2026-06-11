# live

Live stream command line output.

Enable agents to inspect logs via familiar interfaces like `cat` and `tail`.
Explore logs from long-running or long-dead processes.

Large logs auto-rotate. Old sessions are cleaned opportunistically.
No long running daemons. All state is stored in `~/.live`.

Requires Python 3.10+. Zero dependencies.

## Install

```bash
pipx install live-cmd
# or
uv tool install live-cmd
```

Install shell completions:

```bash
live update-shell
```

Install [agent skill](https://github.com/astralarium/live/blob/main/skills/live-cmd/SKILL.md):

```bash
npx skills add astralarium/live
```

Print [agent guide](https://github.com/astralarium/live/blob/main/REFERENCE.md#agent-guide):

```
live llms.txt
```

## Usage

Record command:

```bash
live run -n server npm start     # foreground
live run -d -n server npm start  # detached; prints session UUID
```

Inspect sessions:

```bash
live ls              # active sessions
live ls -ag          # all sessions
live less server     # interactive paging
live tail -f server  # follow logs
```

Select by name (newest match) or UUID prefix.
Commands are scoped to sessions in the current directory (and descendants); pass `-C PATH` to scope to another directory, or `-g` for global scope.

Stop and clean up:

```bash
live stop server
live rm server
live rm --exited --older-than 1d
```

## Agents

Resumable streaming for agents using POSIX semantics:

```bash
live cat -v server
```

Verbose output (`-v`) returns stream metadata on stderr.

```
live: id=925f… next-line=42 next-byte=250 last-time=1800…
```

Continue reading from next line:

```bash
live tail -vn +42 server
```

## Config

`~/.live/config.json`, auto-created.

| Option         | Default | Description                                                         |
| -------------- | ------- | ------------------------------------------------------------------- |
| `ttlDays`      | `7`     | Time before old sessions are cleaned up. Negative value to disable. |
| `maxKb`        | `512`   | Per-session output cap, in KB. Older segments are dropped.          |
| `segmentKb`    | `64`    | Segment file size, in KB, before rotation.                          |
| `heartbeatSec` | `30`    | Seconds between writer heartbeats; 3× this marks a session hung.    |

## References

- [REFERENCE.md](https://github.com/astralarium/live/blob/main/REFERENCE.md)
- [DESIGN.md](https://github.com/astralarium/live/blob/main/DESIGN.md)
- [DEVELOPMENT.md](https://github.com/astralarium/live/blob/main/DEVELOPMENT.md)

## Links

- [GitHub: `astralarium/live`](https://github.com/astralarium/live)
- [PyPI package: `live-cmd`](https://pypi.org/project/live-cmd/)
