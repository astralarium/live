# live

Live stream command line output. Inspect long processes from agent workflows.

Requires Python 3.14+. Zero dependencies.

## Install

```sh
pipx install live-cmd
# or
uv tool install live-cmd
```

Shell completions:

```sh
live update-shell        # detect $SHELL, install completions
```

Agent guide:

```sh
live llms.txt
```

## Usage

```sh
live run -n dev npm run dev   # record under PTY, mirror to terminal
# switch terminals / agent tooling
live ls                       # list sessions started under cwd
live tail -vn +0 dev          # resumable polling for agents
live cat dev                  # full output
live rm dev                   # delete
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
