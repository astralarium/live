# `live` — a log-tailing MCP server

## What it is

A CLI wrapper plus a tiny MCP server, coupled only through the filesystem — **no daemon, no broker**. The user runs any command prefixed with `live`; it runs the command normally (output still visible in their terminal) while recording it to disk in the nearest `.live/` walking up from the cwd. An agent's MCP client spawns a server which tells the agent where each session's log files live, so the agent reads them with its existing shell tools (`grep`, `tail`, `awk`). The server's only ongoing job is **cursor translation** for resumable polling — every other read-shape (filter by pattern, head/tail, ANSI stripping, time windows) is the agent's existing toolbox applied to a path.

The `live` process is the sole writer of a session's files; the MCP server only reads. Per-conversation cursor positions live in the MCP server process's memory and disappear when the connection closes — no daemon state, no persistence between conversations. Putting sessions under a project-scoped `.live/` means project A and project B don't see each other by default, and a monorepo whose root has a single `.live/` aggregates everything beneath it.

## Commands

- `live <cmd…>` — run `<cmd>` under a PTY, mirror its output to the user's terminal, record it.
- `live --mcp` — start the MCP server on stdio. Spawned by the agent's client; one server process per MCP client connection, handling many tool calls over that connection's lifetime then exiting with the client. Not per-call: Node cold-start would dominate.
- `live --completion <shell>` — print the completion script (`bash`/`zsh`/`fish`) to stdout and exit. See **Tab completion**.
- `live -- <cmd…>` — POSIX double-dash: everything after `--` is the command, no flag interpretation. Escape hatch for the rare case of a command whose name collides with a `live` flag (`live -- --completion bash` runs `--completion`). Covers any future flags too.

The goal is "what a human saw" — full stop. PTY-merged is the only capture mode: plain pipes make tools strip color, progress bars, and prompts, defeating the design north star. Stream-of-origin tagging (stdout vs stderr) is left to the program — most tools that emit meaningful content to stderr already self-mark it (`ERROR:`, `WARN:`), and agents grep on that.

## Tab completion

`live <cmd…>` defers completion to `<cmd>` so the wrapped tool's completion comes through — `live git che<TAB>` → `live git checkout`. The wrapper is invisible at the completion layer.

**Handoff rule.** Before any non-flag argument, offer `live`'s own flags (just `--completion`). After a non-flag token or `--`, completion defers entirely to the wrapped command.

**Per-shell mechanism**, using each shell's existing "complete as command" facility:

- **bash**: `complete -F _live live` (uses `_command_offset` after consuming flags)
- **zsh**: `compdef _live live` (defers via `_normal`)
- **fish**: `complete -c live -x -a '(__fish_complete_subcommand)'`

**Installation.** `live --completion <shell>` prints the script:

```
live --completion bash > ~/.local/share/bash-completion/completions/live
live --completion zsh  > "${fpath[1]}/_live"
live --completion fish > ~/.config/fish/completions/live.fish
```

## On-disk layout

Discovery is git-style: both `live` and the MCP server walk **up** from their cwd looking for the nearest `.live/`. First hit wins; that directory anchors writes (for `live`) and reads (for the MCP server). If walk-up reaches `/` without finding one, the **fallback is `~/.live/`** — auto-created on first use — so `live foo` invoked anywhere always has somewhere to land. The home `.live/` doubles as the global housekeeping root (config lives there too).

```
<project>/.live/                # found by walk-up, or fallback ~/.live/
  .gitignore                    # auto-created with `*` on first run, so `git add .` stays clean
  includes                      # optional plain-text file, one relative path per line — see below
  sessions/
    <id>/                       # <id> is a ULID — lexicographic sort = chronological sort
      meta.json                 # see below
      stream.0003.log           # FROZEN — oldest surviving segment
      stream.0004.log           # FROZEN
      ...                       # typically ~8 live segments at steady state
      stream.0010.log           # CURRENT — writer appends here
      lines.0003.log            # JSONL {n,t} per complete line in segment 0003
      lines.0004.log
      ...
      lines.0010.log
      process.lock              # flock'd exclusive for the live process's whole lifetime
      deadAt                    # post-mortem marker; mtime is the TTL clock, content is the consistency verdict
```

Both `stream` and `lines` are split into numbered, zero-padded segments. The writer only appends to the highest-numbered segment pair; once closed, a segment never changes until retention `unlink`s it. Old data is structurally immutable — see **Segments & retention** for rotation, retention, and the residual race.

**Explicit child inclusion via `.live/includes`.** A monorepo can opt a root `.live/` into aggregating child `.live/` dirs. Format is plain text, one relative path per line:

```
apps/web
apps/api
packages/ui
```

Paths must resolve to **subdirectories** of the `.live/`'s parent. Validation: `realpath` both the candidate (`<parent>/<entry>/.live/`) and the parent, then verify the candidate's resolved path segments start with the parent's. Segment-prefix matching (not string `startsWith`) avoids the `secret`/`secret-leak` collision, and the `realpath` step catches symlink escape that lexical checks miss. Invalid entries are skipped with a stderr warning.

The MCP server resolves `includes` **transitively** — an included `.live/`'s own `includes` is followed. Containment is enforced, so every edge points strictly downward and the graph is a DAG by construction (cycles impossible). A visited-set keyed by realpath dedupes diamonds and catches any residual symlink loops.

Common pattern: a workspace root `.live/` with empty `sessions/` and a populated `includes` is an aggregation point; each package's `.live/` is where sessions get written.

`meta.json`:

```json
{
  "id": "…",
  "command": ["pnpm", "dev"],
  "cwd": "/abs/realpath",
  "startedAt": 1717200000000,
  "exitedAt": null,     // ms epoch on graceful exit; null otherwise (crash → derive from mtime(deadAt))
  "status": "running",  // or "exited" — display only; TTL clock is deadAt's mtime
  "exitCode": null,
  "firstSegment": 0,    // lowest-numbered segment still on disk
  "lastSegment": 2      // highest, == the current (being-appended) segment
}
```

`meta.json` is **writer-only** and near-immutable: written at session start, rotation (`lastSegment`++), retention (`firstSegment`++), and graceful exit (`status`, `exitCode`, `exitedAt`). Sweepers don't touch it. Writes are atomic — write-to-temp + rename — so partial reads never happen. Absolute line numbers derive from segments on demand: `firstLine` = first `n` in `lines.<firstSegment>.log`; `lastLine` = last complete `n` in `lines.<lastSegment>.log`; `count = lastLine - firstLine + 1`.

Crashed sessions keep last-written values (typically `exitedAt: null`, `status: "running"`, `exitCode: null`). The MCP server resolves the exposed `exitedAt` by precedence: `meta.json.exitedAt` if set, else `mtime(deadAt)`.

**Empty states.** Three edge cases the derivation has to handle:

- **Brand-new session, no records yet** (`lines.<firstSegment>.log` is empty): report `firstLine: 1, lastLine: 0, count: 0`. Standard "empty range" sentinel — `count` formula yields 0 cleanly.
- **Just-rotated current segment** (`lines.<lastSegment>.log` is empty but earlier segments have records): walk backward from `lastSegment` until a segment with a complete record is found; use its last record as `lastLine`. In practice this only steps once.
- **Truncated trailing record** in any `lines.NNNN.log` (writer was `SIGKILL`'d mid-write): scan to the last `\n` and ignore anything after it. Standard JSONL behavior — readers tolerate a partial trailing record.

## Recording

`live` runs the child under a PTY, reads the master fd, writes the bytes straight to its own stdout (the passthrough the user sees) and appends the same bytes to the current `stream.NNNN.log` segment. Plumbing: child in its own process group (Ctrl-C reaches it), `SIGWINCH` forwarded to resize the PTY.

Line indexing: when the first byte of a new line is written, capture `t` (ms epoch). On the newline, append one record `{"n": <absolute>, "t": <t>}` to the current `lines.NNNN.log`. A trailing partial line (bytes after the last `\n`) gets **no** record until its newline arrives — so readers only ever see complete lines, and reads stay lock-free against the appending writer.

**Write order per line: stream first, then lines.** Bytes append to `stream.NNNN.log`, then the `{n,t}` record appends to `lines.NNNN.log`. Invariant: `lines.*.log` records are always a prefix of `stream.*.log` complete lines. A crash between the pair leaves one extra complete line in `stream` with no matching `lines` record — never the reverse — so the consistency check (see **Liveness & cleanup**) only has to look at the trailing edge of `stream`.

This `{n,t}` is the whole metadata layer and satisfies "timestamp per line" with no seek machinery: across the concatenation of all `stream.*.log`, line `i` pairs with the `i`-th `{n,t}` record across the corresponding `lines.*.log`. Absolute `n` survives segment deletion — retention just raises the effective floor (the lowest surviving `n`); nothing gets renumbered, and no field has to be updated to record the new floor.

## Segments & retention

Two thresholds: `segmentKb` (default **64 KB**) controls rotation, `maxKb` (default **512 KB**) caps total retained per session. Picked so a session keeps roughly 8 live segments — small enough that retention deletes in fine grain, large enough that the cursor's segment-lookup is cheap.

**Rotation.** When the writer finishes a line (newline appended) and the current segment is at-or-past `segmentKb`, it closes the segment pair and increments `lastSegment`. Lines never split across segments — they finish in whatever segment they started in, even if that overshoots the threshold (a 2 MB log line just produces a fat 2 MB segment).

**Retention.** After each rotation, the writer sums the bytes of all `stream.*.log` and, while the total exceeds `maxKb`, `unlink`s the lowest-numbered segment pair and bumps `firstSegment` in `meta.json`. The effective `firstLine` floor advances implicitly — the new first segment's first record reflects it.

**Concurrency.** Frozen segments are immutable, so readers open them without coordination. The only ongoing mutations are appends to the current segment (POSIX-safe against concurrent readers) and `unlink`s of oldest segments at retention. POSIX semantics handle the latter: if a reader already `open()`d a segment, an `unlink` doesn't invalidate the read — the inode persists until the last fd closes.

**Residual race.** Between `cursor` returning a segment list and the agent's `cat` opening those segments, retention could `unlink` the lowest one. `cat`'s `open()` then returns `ENOENT`, the computed skip is off, and the agent's `tail -n +K+1` lands wrong. Two saving graces: the failure mode is a visible error (detectable via `${PIPESTATUS[0]}`, not a silent misread), and the next poll's `cursor` returns a fresh segment list with `gap: true` if any tracked line was dropped. Defensive agents retry on non-zero; most accept one poll's worth of stale data.

## Liveness & cleanup

**Liveness = the `process.lock` flock is still held.** `live` takes an exclusive flock on `process.lock` at startup and holds it for its lifetime; the kernel releases it on any exit — clean, crash, or SIGKILL. This is immune to pid reuse (a recorded pid coming back as an unrelated process) and correctly classifies a SIGKILL'd `live` whose `meta.json` still says `running` as dead.

**Cleanup sweep**, run opportunistically on every `live` startup (scans its own `.live/sessions/`) and on every MCP `list_sessions` call (sweeps the anchoring `.live/` and every `.live/` transitively reachable through `includes`). Each sweep is scoped to one `.live` directory; concurrent sweeps across different projects are independent, and concurrent sweeps within the same project are safe via the `O_EXCL` note below:

```
for each session in this .live/sessions/:
  if  process.lock NOT held  AND  no deadAt yet:
      create deadAt              # open O_EXCL: first sweeper wins, others get EEXIST and skip
  if  process.lock NOT held  AND  now − mtime(deadAt) > ttlDays:
      delete session             # a racing ENOENT from another sweeper is ignored
```

The TTL clock reads exactly one file: `deadAt`'s mtime. It's stamped two ways, same marker either path — graceful exit creates `deadAt` itself during shutdown (mtime ≈ real exit time); a crash/SIGKILL gets it from the first sweeper that observes the released lock. The `O_EXCL` create is race-free, so concurrent sweepers never double-write and there's no read-modify-write on `meta.json`.

**Consistency check on first death observation.** When the sweeper creates `deadAt`, it counts complete lines in `stream.<lastSegment>.log` and records in `lines.<lastSegment>.log`. Equal → `deadAt` is empty (consistent). `stream` has one more → `deadAt` contains `inconsistent\n` (the SIGKILL-mid-record-write case). The verdict is permanent. Cost: one streaming read of the last segment (≤ `segmentKb`), at most once per session.

Graceful exits don't run the check — `live` writes its last `{n,t}` record before releasing `process.lock`, so the prefix invariant guarantees consistency. `live` stamps `deadAt` empty itself during shutdown.

The liveness gate is absolute: **a live session is never cleaned**, however stale (idle dev server, quiet `tail -f`). No heartbeat — the flock flips to dead on crash for free, and `deadAt` supplies the timestamp on first observation. For crashes, TTL counts from first-detection-of-death rather than actual death — looser than spec but strictly safer (logs kept longer, never deleted early). Sweeps fire on every `live` startup and every `list_sessions`, so detection is normally prompt.

## Config

`~/.live/config.json`, auto-created with defaults on first run (global — these are user-level housekeeping knobs, not project policy). Lives alongside the home `.live/` fallback sessions root:

```json
{ "ttlDays": 7, "maxKb": 512, "segmentKb": 64 }
```

No environment variables.

## MCP tools

Two read tools. `list_sessions` hands out paths and watermarks; `cursor` advances a per-conversation position and returns the segments + skip needed to read new content. Everything else is shell. There is **no `stop_session`** — agents observe; termination stays with the user (Ctrl-C, `kill`).

**Cursor state.** The MCP server holds an in-memory map `(path, session_id) → lastLine` per connection. A new entry is placed at the session's current `lastLine` on the first `cursor` call for that session; subsequent calls advance it as part of returning. The map dies with the connection.

### `list_sessions`

Params:

- `include_exited: bool` (default `false`) — live sessions only by default; `true` includes exited.

Walks the discovery tree (see **On-disk layout**) and returns one entry per reachable session. Triggers a cleanup sweep on each `.live/` visited.

Each entry:

- `id` — ULID
- `command` — argv `live` was invoked with
- `cwd` — absolute path `live` was invoked from
- `status` — `"running"` or `"exited"`
- `consistent: bool` — `false` if the writer was SIGKILL'd mid-record-write, leaving one trailing stream line with no `{n, t}` record. Implicit `true` for live sessions; sourced from `deadAt` content for dead ones
- `startedAt`, `exitedAt?`, `exitCode?`
- `path` — absolute path to the session directory; agent reads `<path>/stream.*.log` and `<path>/lines.*.log` directly with shell tools
- `firstSegment`, `lastSegment` — from `meta.json`
- `firstLine`, `lastLine`, `count` — retained range and total complete-line count, derived from segment files (no cache)

`lastLine` is informational — useful for display or as a starting value for an explicit `since_line` override. The normal polling path doesn't thread it back; `cursor` tracks position per-conversation.

### `cursor` — the only stateful tool

Params:

- `path` (required) — session directory from `list_sessions`. Skips re-walking the inclusion graph to locate it
- `session_id` (required) — ULID, sanity-checked against `<path>/meta.json`
- `since_line: N` (optional) — explicit position. Overrides the tracked cursor and updates it to the result. Omitted → server uses its tracked cursor; on first use for a session, placed at current `lastLine`

Returns:

- `segments` — ordered filenames (e.g. `["stream.0003.log", "stream.0004.log"]`) to concatenate, relative to `path`. Explicit snapshot at call time so the agent isn't racing the glob against retention
- `skip_lines` — file-relative skip count against the concatenation of `segments`. Agent reads with `cat <path>/{segments…} 2>/dev/null | tail -n +$((skip_lines + 1))`
- `last_line` — cursor's new position. Informational; the server tracks it
- `gap: bool` — true when the effective `since_line` is below current `firstLine` (cursor predates retained data)

**Algorithm.** Read `meta.json` for the segment range, then forward-scan `lines.<firstSegment>.log` … `lines.<lastSegment>.log`, opening each just long enough to read the first record's `n`. Stop at the first segment K where `n_first_K > since_line`; segment K-1 contains line `since_line + 1`. If the scan exhausts without finding such a K, `since_line` is in `lastSegment` (or beyond) — treat K-1 as `lastSegment`. `skip_lines = since_line - n_first_{K-1} + 1`. `segments` is `stream.<K-1>.log` through `stream.<lastSegment>.log`. `last_line` is the last complete record's `n` in `lines.<lastSegment>.log`, walking back to the previous non-empty segment if the current is empty. At ~8 segments the scan is sub-millisecond; early-exit makes the common "recent cursor" case touch a couple of files.

**Session no longer exists.** If `meta.json` is missing (session was TTL-cleaned mid-conversation, or path was bogus), the call returns an MCP error (`INVALID_PARAMS`) with a message naming the session, so the agent drops the cached cursor entry and re-discovers via `list_sessions`.

**Edge cases**:

- **First call** (no tracked cursor, no override): cursor placed at current `lastLine`. Return `segments: []`, `skip_lines: 0`, `last_line: lastLine`, `gap: false`. Subsequent calls return new content.
- **Caught up** (`since_line >= lastLine`): return `segments: []`, `skip_lines: 0`, `last_line: lastLine`, `gap: false`.
- **Gap** (`since_line < firstLine`): return everything still on disk with `segments: [stream.<firstSegment>.log, …, stream.<lastSegment>.log]`, `skip_lines: 0`, `gap: true`.
- **Empty session** (`lastLine: 0`): degenerate caught-up. `segments: []`, `gap: false`.
- **Empty current segment** (just rotated): `segments` ends at the last segment with ≥ 1 complete record.

### Agent-facing descriptions

The strings the SDK registers — these are contractual API surface, since they're what every agent reads to discover the protocol.

**Server `instructions`**:

> `live` records terminal sessions to disk. Use `list_sessions` to discover sessions. For ad-hoc reads, run shell tools (`cat`, `tail`, `grep`) on `<path>/stream.*.log` directly. For resumable polling on new content, call `cursor` — it tracks your position per-conversation and returns just the new lines on each call.

**`list_sessions` description**:

> List sessions visible from the current directory and its `.live/includes` reachable set. Live sessions only by default; pass `include_exited: true` for exited ones too.
>
> Each entry has a `path` — read `<path>/stream.*.log` directly with shell tools:
>   - `cat <path>/stream.*.log | tail -n 200` — recent output
>   - `grep ERROR <path>/stream.*.log` — scan for errors
>   - per-line timestamps in `<path>/lines.*.log` as JSONL `{n, t}`
>
> For polling on new content, use `cursor(path, session_id)`.
>
> `consistent: false` means the writer was killed mid-record-write; one trailing stream line has no `{n, t}` record (visible in raw reads, not tracked by `cursor`).

**`cursor` description**:

> Returns the segments and skip count to read new lines from a session. The server tracks your position per `(path, session_id)` for this MCP connection's lifetime.
>
> Read the result with:
>   `cat <path>/{segments…} 2>/dev/null | tail -n +$((skip_lines + 1))`
>   (prepend `<path>/` to each filename in `segments`)
>
> First call places the cursor at current `lastLine` and returns `segments: []` (backlog skipped). For backlog, read `<path>/stream.*.log` directly first.
>
> `segments: []` on later calls: nothing new — poll again.
>
> `gap: true`: retention dropped tracked lines. `segments` is everything still on disk; the range below current `firstLine` is unrecoverable.
>
> Optional `since_line` overrides the tracked cursor — useful to backfill, replay, or recover from a gap.

**Parameter descriptions**:

- `list_sessions.include_exited`: "If true, include sessions whose `live` process has ended. Default false (live sessions only)."
- `cursor.path`: "Absolute path to the session directory, from `list_sessions`."
- `cursor.session_id`: "ULID from `list_sessions`. Verified against the session's `meta.json`; the call fails if they disagree."
- `cursor.since_line`: "Optional override. When omitted, the server uses its tracked cursor for this `(path, session_id)`. When provided, reads from that line forward and updates the tracked cursor to match."

## Implementation

TypeScript / Node. Dependencies:

- `@modelcontextprotocol/sdk` — MCP server.
- `node-pty` — PTY for the wrapper.
- `ulid` — session IDs. Crockford-base32, lexicographically sortable, ~3 KB, zero deps.
- A POSIX flock primitive for `process.lock` — the only lock the design uses. Node's built-in `fs` doesn't expose `flock(2)`; a tiny native binding or a thin package like `fs-ext` works.

One published `bin` entry (`live`) that branches on invocation at argv parse time:

- `--mcp` → start MCP server on stdio (terminating internal flag).
- `--completion <shell>` → print completion script and exit (terminating internal flag).
- `--` → consume, treat the rest of argv as the command.
- otherwise → wrap the rest of argv as the command.

MCP client config:

```json
{ "mcpServers": { "live": { "command": "live", "args": ["--mcp"] } } }
```

**Platform stance**: POSIX for v1 (Linux + macOS, WSL works on Windows). `node-pty` supports Windows but `flock(2)` semantics don't, so supporting Windows natively means swapping to a cross-platform locking library (e.g. `proper-lockfile`) and verifying lock-release-on-process-death actually fires. Deferred until there's clear demand.

## Settled defaults at a glance

| Thing             | Value                                                                  |
| ----------------- | ---------------------------------------------------------------------- |
| Session location  | nearest `.live/` walking up from cwd; fallback `~/.live/`              |
| Cross-project view| `.live/includes` lists subdirectory `.live/` dirs to aggregate (opt-in)|
| Capture           | PTY, merged stdout+stderr                                              |
| TTL               | 7 days, from `deadAt` mtime (first dead-detection), dead sessions only |
| Segment size      | 64 KB rotation threshold (`segmentKb`); lines never split across segments |
| Retention         | 512 KB total per session (`maxKb`); oldest segments `unlink`'d when over |
| MCP entry         | `live --mcp` (terminating internal flag)                               |
| MCP surface       | `list_sessions` (paths + watermarks), `cursor` (segment list + skip)   |
| Reading text      | Agent uses `cat <path>/stream.*.log \| tail`/`grep`/`awk`/`sed`        |
| Liveness          | held flock on `process.lock`                                           |
| Config            | `~/.live/config.json` (`ttlDays`, `maxKb`, `segmentKb`)                |
