# live

Live stream command line output. Inspect long processes from agent workflows.

Requires Python 3.10+. Zero dependencies.

## Install

```bash
pipx install live-cmd
# or
uv tool install live-cmd
```

Shell completions:

```bash
live update-shell        # detect $SHELL, install completions
```

Agent guide:

```bash
live llms.txt
```

## Usage

Record any command:

```bash
live run -n server npm start
```

Inspect from another process:

```bash
live cat server
live tail -f server
```

Manage session recordings:

```bash
live ls
live rm server
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

- [PyPI package: `live-cmd`](https://pypi.org/project/live-cmd/)
