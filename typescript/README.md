# live

A log-tailing MCP server. Wrap any command with `live` to mirror its full PTY output to your terminal and record it to disk. An MCP server exposes those recordings to agents.

## Install

```sh
npm install -g @astralarya/live
```

Requires Node 20+. POSIX only (Linux, macOS, WSL). Native bindings (`node-pty`, `fs-ext`) build on install.

## Usage

```sh
live npm run dev                # see output as normal; recorded under nearest .live/
live -n dev npm run dev         # tag the session so the agent can filter for it
```

`--name <name>` / `-n <name>` attaches a label that shows up in MCP `list_sessions`; pass `name` as a filter input to scope the result to matching sessions.

### Project scope

Live stores session recordings in the nearest `.live/` folder walking up from the working directory.
Store session data in workspace `.live/` folders to make logs accessible to agent workflows.

For convenience, `live --init` creates a `.live/` folder with a `.gitignore` to ignore session data in the current directory.

## MCP server

Add to your client's config (Claude Code's `~/.claude.json` or `.mcp.json`, Claude Desktop, Cursor, Cline — same shape):

```json
{
  "mcpServers": {
    "live": {
      "command": "npx",
      "args": ["-y", "@astralarya/live@latest", "--mcp"]
    }
  }
}
```

`-y` skips the npx install prompt; `@latest` pulls the freshest version each spawn. If `live` is already globally installed, swap to `"command": "live", "args": ["--mcp"]`.

- `list_sessions` — discover sessions; returns paths + watermarks. Filter by `name` to scope to `--name`-tagged runs.
- `cursor` — resumable polling; returns the segment list and skip count for new lines since the last call.
- ad-hoc reads — run `cat`, `tail`, `grep` directly on `<path>/stream.*.log`.

At startup the MCP server scans the working directory once (skipping `node_modules`, `.git`, `.svn`, `.hg`) and indexes every `.live/` it finds. New `.live/` directories created after the connection started appear on the next MCP start.

## Shell completion

`live <cmd…>` defers tab completion to `<cmd>`. Install for your shell:

```sh
live --completion bash > ~/.local/share/bash-completion/completions/live
live --completion zsh  > "${fpath[1]}/_live"
live --completion fish > ~/.config/fish/completions/live.fish
```

## Config

`~/.live/config.json` (auto-created, defaults shown):

```json
{ "ttlDays": 7, "maxKb": 512, "segmentKb": 64 }
```

Local `.live/` folders may define values in `config.json` to override fields.

Full design in [DESIGN.md](DESIGN.md).

## License

MIT
