# `live` — design

A CLI wrapper plus an MCP server, coupled only through the filesystem. The user prefixes any command with `live`; the wrapper runs it under a PTY, mirrors output to the terminal, and records the same bytes to disk in the nearest `.live/`. The MCP server points agents at session paths so they read with their own shell tools (`cat`, `tail`, `grep`); a `cursor` tool resolves a per-conversation position into a `(segments, skip_lines)` pair for resumable polling.

No daemon, no broker, no persistent server-side state. The wrapper is the sole writer of session content; the MCP server reads sessions and runs lifecycle sweeps (creating `deadAt`, unlinking expired sessions) — never touches `meta.json`, `stream.*.log`, or `lines.*.log`. Cursor positions live in MCP-server-process memory and vanish when the client disconnects.

## CLI

- `live <cmd…>` — wrap `<cmd>` under a PTY, mirror to stdout, record to disk.
- `live -- <cmd…>` — required when `<cmd>` starts with `-`; the `-…` namespace is reserved for `live`'s own flags.
- `live --name <name>` / `-n <name>` — attach a label; surfaced and filterable in `list_sessions`. Not unique.
- `live --init` — create `.live/` plus `.live/sessions/` and `.live/.gitignore` (ignores `sessions/`) in cwd. Idempotent. A bare `mkdir .live` also opts a project in; `--init` just additionally writes the gitignore.
- `live --mcp` — start the MCP server on stdio.
- `live --completion <bash|zsh|fish>` — print the shell completion script.
- `live --help` / `-h` — usage.

Capture is PTY-merged stdout+stderr.

## On-disk layout

Discovery is git-style: walk up from cwd to the nearest `.live/`. If walk-up reaches `/` without finding one, fallback to `~/.live/`, auto-created on first use. The home `.live/` also hosts `config.json`.

```
<project>/.live/
  .gitignore                # created by `live --init`; ignores `sessions/`
  config.json               # optional per-project override
  sessions/                 # session record data
    <ulid>/
      meta.json
      process.lock          # flock'd exclusive for the recorder's lifetime
      deadAt                # post-mortem marker; mtime = TTL clock, content = verdict
      stream.0000.log       # raw bytes; one or more frozen + one current
      stream.0001.log
      lines.0000.log        # JSONL `{n, t}` per complete line in stream.NNNN.log
      lines.0001.log
```

The recorder only creates `sessions/` if it's missing — nothing else under `.live/` is auto-populated. `--init` writes the gitignore. The only file the tool maintains outside a project's `.live/` is `~/.live/config.json`.

ULID session IDs sort lexicographically by creation time. Both `stream` and `lines` are split into zero-padded numbered segments; the recorder appends only to the highest-numbered pair, and frozen segments are immutable until retention unlinks them.

### `meta.json`

```json
{
  "id": "01JC…",
  "command": ["pnpm", "dev"],
  "cwd": "/abs/path",
  "name": "dev",
  "startedAt": 1717200000000,
  "exitedAt": null,
  "status": "running",
  "exitCode": null,
  "firstSegment": 0,
  "lastSegment": 2
}
```

`name` is present only when the user passed `--name`/`-n`.

`meta.json` is writer-only — written at session start, every rotation (`lastSegment++`), every retention drop (`firstSegment++`), and graceful exit. Writes are atomic (write-to-temp + rename).

Absolute line numbers derive from the segment files on demand:

- `firstLine` = first `n` in `lines.<firstSegment>.log`
- `lastLine` = last complete `n` in `lines.<lastSegment>.log` (walk back if the current segment is empty)
- `count` = `lastLine − firstLine + 1`

The MCP server resolves `exitedAt` for the wire by precedence: `meta.json.exitedAt` if set (graceful exit), else `mtime(deadAt)` (crash).

### Cross-project aggregation

The MCP server walks `serverCwd` once at startup, recursive `readdir`, and indexes every `.live/` it finds. Discovery and scope are the same thing: a `.live/` is visible iff it lives under the MCP client's working directory.

Walker rules: don't descend into a `.live/` once found; skip `node_modules`, `.git`, `.svn`, `.hg`; don't follow symlinks (`Dirent.isDirectory()` is false for them, so cycles can't form); silently skip unreadable subtrees.

The scan result is cached for the connection's lifetime. New `.live/` directories created mid-session appear on the next MCP start. So `list_sessions` from a project root sees that project plus descendants; from `~/`, every project under home; from outside any project, `[]`.

## Recording

The recorder runs the child under a PTY, reads the master fd, writes the bytes straight to its own stdout, and appends the same bytes to the current `stream.NNNN.log`. Child is in its own process group so Ctrl-C reaches it; `SIGWINCH` is forwarded to resize the PTY.

### Line indexing

- When the first byte of a new line arrives, capture `t = Date.now()`.
- On the terminating newline, append `{"n": <absolute>, "t": <captured-t>}` to `lines.NNNN.log`.
- A trailing partial line gets no record until its newline arrives, so readers only see complete lines and stay lock-free against the appending writer.

`n` is an absolute counter across the session's lifetime. Retention deletes segments but never renumbers; the floor advances implicitly via the new oldest segment's first record.

### Write-order invariant

For each line:

1. The byte range is appended to `stream.NNNN.log`.
2. The matching `{n, t}` record is appended to `lines.NNNN.log`.

This gives the **prefix invariant**: `lines.*.log` records are always a prefix of the complete lines in `stream.*.log`. A crash between the two writes leaves one extra complete line in `stream` with no `lines` record — never the reverse. The consistency check only has to inspect the trailing edge.

If a `lines` write fails mid-session (disk full, EIO), the recorder kills the PTY and stamps `deadAt` with the `inconsistent` verdict on exit. Silently diverging from what the user already saw on the terminal isn't an option.

## Segments and retention

Two thresholds, both per-project-configurable:

- `segmentKb` (default 64) — rotate after a completed line carries the active stream segment past this.
- `maxKb` (default 512) — total retained `stream.*.log` bytes per session.

### Rotation

When the recorder finishes a line and the active segment is at-or-past `segmentKb`, it closes the active pair and opens a new one. Lines never split across segments — a single line larger than `segmentKb` produces a fat segment and rotates after.

Rotation order on disk: `meta.lastSegment++` and atomic meta write happen **before** opening the new segment files. A crash inside this window leaves meta naming a not-yet-existing segment, which readers tolerate as empty and walk back from. The reverse order would orphan new segment files outside meta's range — invisible to readers, only reclaimed at TTL.

### Retention

After each rotation, the recorder sums the bytes of all `stream.*.log` and, while the total exceeds `maxKb`, `unlink`s the lowest-numbered segment pair and bumps `firstSegment` in `meta.json`. Frozen segments are immutable, so readers open them without coordination.

### Residual reader race

Between `cursor` returning a segment list and the agent's `cat` opening those files, retention could `unlink` the lowest one. POSIX `open()` then returns `ENOENT` — a visible error, not a silent misread. The next poll's `cursor` returns a fresh segment list with `gap: true` if any tracked line was dropped. Defensive agents retry on non-zero pipeline status; most accept one poll's worth of stale data.

## Liveness and cleanup

**Liveness** = the recorder still holds an exclusive flock on `process.lock`. The kernel releases the lock on any process exit — clean, crash, or SIGKILL — so liveness is decided by trying to acquire the lock non-blocking from a probe fd. Acquired → dead. EAGAIN/EWOULDBLOCK → alive. The probe immediately releases. Immune to pid reuse; a SIGKILL'd recorder whose `meta.json` still says `running` is correctly classified dead.

### Sweep

Runs opportunistically on every `live` startup (scans its own `.live/sessions/`) and on every MCP `list_sessions` call (sweeps each discovered `.live/`). The cursor tool fires its own rate-limited sweep on the owning `.live/` so polling-only agents still observe neighbor deaths.

```
for each session in this .live/sessions/:
  if process.lock NOT held AND no deadAt yet:
      create deadAt (O_EXCL)         # first sweeper wins; others see EEXIST
  if process.lock NOT held AND now − mtime(deadAt) > ttlDays:
      delete session                  # parallel ENOENT is benign
```

`O_EXCL` makes concurrent sweepers safe without coordination — at most one creates `deadAt`; the rest see `EEXIST` and skip.

### `deadAt` marker

- **Empty file** = `consistent`. The recorder reached graceful exit (final `{n,t}` written before flock release).
- **`"inconsistent\n"`** = the writer was SIGKILL'd between writing a `stream` line and the matching `lines` record, or an in-session disk error tore the recording from the terminal output.

Graceful exit stamps `deadAt` directly (no streaming check). Sweepers compute the verdict on first observation by counting complete lines in `stream.<lastSegment>.log` vs records in `lines.<lastSegment>.log` — a single pass over the last segment pair. Equal → consistent; stream one ahead → inconsistent; any other diff violates the write-order invariant and is logged.

### TTL

`deadAt`'s mtime is the TTL clock. For crashes, TTL counts from first-detection-of-death rather than actual death — strictly safer (logs kept longer, never deleted early). A live session is never cleaned, however stale.

## Configuration

`~/.live/config.json` is auto-created on first run with defaults:

```json
{ "ttlDays": 7, "maxKb": 512, "segmentKb": 64 }
```

Any `.live/` may carry its own `config.json` to override fields. Layering is per-field: per-`.live/` over home over compiled defaults. Partial files are valid. The owning `.live/`'s config governs its own sessions, so retention stays authoritative per project even when an aggregating cwd sweeps nested descendants.

Validation: `ttlDays >= 0`, `maxKb` and `segmentKb` strictly positive, all finite. Bad values throw on load.

Asymmetric error policy:

- A malformed **per-project** `config.json` is logged and the file ignored — one bad project shouldn't break sweeps across an aggregated workspace.
- A malformed **home** `config.json` throws. The recorder catches it, warns, and falls back to compiled defaults so the user's command still runs. The MCP server surfaces it as an MCP error.

No environment variables.

## MCP server

Two read tools. `list_sessions` hands out paths and watermarks; `cursor` advances an opaque per-conversation position and returns segments + skip needed to read new content. Everything else is shell. There is no `stop_session` — agents observe; termination stays with the user.

The agent-facing tool descriptions are contractual API surface and live in [src/mcp/server.ts](src/mcp/server.ts).

### `list_sessions`

Parameters:

- `include_exited: bool` (default `false`) — live sessions only by default.
- `name: string` (optional) — filter to sessions whose `meta.name` equals this string. Results stay ULID-sorted, so the first match is the most recent.

Iterates the `.live/` directories the MCP server discovered at startup (one filesystem scan from `serverCwd`). Returns one entry per session across the set, sorted by `id` descending (ULID lex desc = chronological desc, i.e. newest first). Triggers a cleanup sweep on each `.live/` it visits. A malformed home `config.json` surfaces as an MCP error (per the asymmetric error policy below).

Each entry:

- `id`, `command`, `cwd`, `startedAt`
- `name?` — present iff the session was started with `--name`.
- `status` — `"running"` or `"exited"`
- `consistent: bool` — `true` for live sessions; for dead ones, derived from `deadAt` content. `false` means a write was torn at the trailing edge (see **Liveness and cleanup**).
- `exitedAt?` — present if known. Graceful exit writes `meta.exitedAt`; for crashes, derived from `mtime(deadAt)`.
- `exitCode?` — present if the recorder wrote it (graceful exit).
- `path` — absolute path to the session directory. Agents read `<path>/stream.*.log` directly.
- `firstSegment`, `lastSegment` — from `meta.json`.
- `firstLine`, `lastLine`, `count` — derived from segment files.

`lastLine` is informational. Normal polling threads no state back to the server; `cursor` tracks it per-conversation.

### `cursor`

The only stateful tool. State is two maps held per MCP connection: a `(path, session_id) → last-returned-line` cursor map, plus a per-`.live/` sweep cooldown map.

Parameters:

- `path` (required) — session directory from `list_sessions`.
- `session_id` (required) — ULID; sanity-checked against `<path>/meta.json`.
- `since_line: N` (optional) — explicit override. Overrides the tracked cursor and updates it to the result. Omitted → server uses its tracked cursor; on first use for a session, placed at current `lastLine`.

Returns:

- `segments: string[]` — ordered filenames (e.g. `["stream.0003.log", "stream.0004.log"]`) to concatenate, relative to `path`. Explicit snapshot at call time so the agent isn't racing a glob against retention.
- `skip_lines: number` — file-relative skip against the concatenation of `segments`. Agents read with `cat <path>/{segments…} 2>/dev/null | tail -n +$((skip_lines + 1))`.
- `last_line: number` — cursor's new position; informational.
- `gap: bool` — `true` when the effective `since_line` is below the current `firstLine` (cursor predates retained data).

#### Algorithm

Forward-scan `lines.<firstSegment>.log` … `lines.<lastLineSegment>.log` (where `lastLineSegment` is the last segment with records — bounding at `meta.lastSegment` would risk a just-rotated empty current segment). Open each just long enough to read its first record's `n`. Stop at the first segment K where `n_first(K) > since_line + 1`. Segment K-1 contains line `since_line + 1`. The boundary `n_first(K) = since_line + 1` collapses to K-1 = K with `skip_lines = 0`, so the comparison is `> since_line + 1`, not `>`.

`skip_lines = since_line − n_first(K-1) + 1`.

#### Edge cases

- **First call** for a `(path, session_id)`: place cursor at current `lastLine`, return `segments: []`. Backlog is the agent's job via direct file reads.
- **Caught up** (`since_line >= lastLine`): return `segments: []`.
- **Gap** (`since_line < firstLine`): return every retained segment with ≥ 1 complete record, `skip_lines: 0`, `gap: true`.
- **Empty session** (`lastLine: 0`): degenerate caught-up.
- **Session gone** (`meta.json` missing — TTL-cleaned mid-conversation or bogus path): return MCP `INVALID_PARAMS` with a message naming the session, so the agent drops the cached cursor entry and re-discovers via `list_sessions`.

## Shell completion

`live <cmd…>` defers tab completion to `<cmd>` so the wrapped tool's completion comes through (`live git che<TAB>` → `live git checkout`). The wrapper is invisible at the completion layer.

**Handoff rule.** Before any non-flag argument or `--`, offer `live`'s own flags. After a non-flag token or `--`, defer entirely to the wrapped command's completion.

Per-shell mechanism uses each shell's existing "complete as command" facility:

- **bash**: `complete -F _live live`; `_command_offset` after consuming flags. Requires the bash-completion package.
- **zsh**: `compdef _live live`; defers via `_normal`.
- **fish**: `complete -c live -x -a '(__fish_complete_subcommand)'`.

Install with:

```sh
live --completion bash > ~/.local/share/bash-completion/completions/live
live --completion zsh  > "${fpath[1]}/_live"
live --completion fish > ~/.config/fish/completions/live.fish
```

## Implementation

TypeScript on Node 20+. Dependencies:

- `@modelcontextprotocol/sdk` — MCP stdio server.
- `node-pty` — PTY for the wrapper.
- `ulid` — Crockford-base32 session IDs, lexicographically sortable.
- `fs-ext` — POSIX `flock(2)` binding for `process.lock`. The design only relies on kernel lock-release-on-death.
- `zod` — schema validation for MCP tool inputs/outputs.

One published `bin` (`live`) branches at argv-parse time on `--mcp`, `--completion`, `--init`, `--help`/`-h`, `--`, or first non-flag token (which starts the wrapped command).

MCP client config:

```json
{ "mcpServers": { "live": { "command": "live", "args": ["--mcp"] } } }
```

One MCP server process per client connection; the process exits with the client.

**Platform.** POSIX-only (Linux, macOS, WSL); the `flock(2)` semantics liveness depends on are POSIX-specific.

## Defaults

| Thing              | Value                                                                  |
| ------------------ | ---------------------------------------------------------------------- |
| Session location   | nearest `.live/` walking up from cwd; fallback `~/.live/`              |
| Cross-project view | filesystem scan from `serverCwd` at MCP startup; skips `node_modules`, `.git`, `.svn`, `.hg` |
| Capture            | PTY, merged stdout + stderr                                            |
| TTL                | 7 days from `deadAt` mtime, dead sessions only                         |
| Segment size       | 64 KB rotation threshold (`segmentKb`); lines never split              |
| Retention          | 512 KB total per session (`maxKb`); oldest segments unlinked when over |
| MCP entry          | `live --mcp`                                                           |
| MCP surface        | `list_sessions`, `cursor`                                              |
| Reading text       | Agent uses `cat <path>/stream.*.log` piped to `tail`/`grep`/`awk`      |
| Liveness           | held flock on `process.lock`                                           |
| Config             | `~/.live/config.json` plus optional per-`.live/` overrides             |
