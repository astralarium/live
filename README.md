# live

Live stream command line output.

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

By default, old sessions are cleaned up a week after termination.

## References

- [REFERENCE.md](https://github.com/astralarium/live/blob/main/REFERENCE.md)
- [DESIGN.md](https://github.com/astralarium/live/blob/main/DESIGN.md)
- [DEVELOPMENT.md](https://github.com/astralarium/live/blob/main/DEVELOPMENT.md)
