# live

Stream long-lived command output to coding agents.

See [DESIGN.md](DESIGN.md) for the design spec and [DEVELOPMENT.md](DEVELOPMENT.md) for working on `live` itself.

## Install

```sh
pipx install astralarya-live
# or
uv tool install astralarya-live
```

## Usage

```sh
live run -n dev npm run dev   # record under PTY, mirror to terminal
live ls                       # list sessions started under cwd
live ls -g                    # list all sessions
live tail --since 0 dev  # resumable polling for agents
live cat dev                  # full output
live rm dev                   # delete
```

Sessions are stored under `~/.live/sessions/`; `live ls`/`cat`/`tail`/`rm` filter to sessions started in the current directory. Pass `-g` / `--global` to search globally.

## Shell completion

`live completion <bash|zsh|fish>` prints the completion script for that shell.
Install once and reload your shell.

```sh
# bash (requires the bash-completion package)
live completion bash > ~/.local/share/bash-completion/completions/live

# zsh (drop into any directory on $fpath)
live completion zsh > "${fpath[1]}/_live"

# fish
live completion fish > ~/.config/fish/completions/live.fish
```

Completes verbs, per-verb flags, and session names (via `live ls -a --json`).
After `live run`, completion hands off to the wrapped command's own completion —
so `live run git che<TAB>` becomes `live run git checkout`.

