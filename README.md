# live

Live stream command line output.

See [DESIGN.md](DESIGN.md) for the design spec and [DEVELOPMENT.md](DEVELOPMENT.md) for working on `live` itself.

## Install

```sh
pipx install live-stream
# or
uv tool install live-stream
```

## Usage

```sh
live run -n dev npm run dev   # record under PTY, mirror to terminal
live ls                       # list sessions started under cwd
live ls -g                    # list all sessions
live tail -vn +0 dev          # resumable polling for agents
live cat dev                  # full output
live rm dev                   # delete
```

Sessions are stored under `~/.live/sessions/`; `live ls`/`cat`/`head`/`tail`/`rm` filter to sessions started in the current directory. Pass `-g` / `--global` to search globally.

## Shell completion

```sh
live update-shell        # detect $SHELL, install completion, reload your shell
```

Or print the script and place it yourself:

```sh
live completion bash > ~/.local/share/bash-completion/completions/live
live completion zsh  > "${fpath[1]}/_live"
live completion fish > ~/.config/fish/completions/live.fish
```
