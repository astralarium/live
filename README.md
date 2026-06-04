# live

Live stream command line output.

## Install

```sh
pipx install live-cmd
# or
uv tool install live-cmd
```

Install shell completions:

```sh
live update-shell        # detect $SHELL, install completion, reload your shell
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

## References

- [REFERENCE.md](REFERENCE.md)j
- [DESIGN.md](DESIGN.md)
- [DEVELOPMENT.md](DEVELOPMENT.md)
