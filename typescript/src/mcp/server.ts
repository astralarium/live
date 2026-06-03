import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { findLiveDirs } from "../session/discovery.js";
import {
  cursor,
  cursorInputSchema,
  cursorOutputSchema,
  listSessions,
  listSessionsInputSchema,
  listSessionsOutputSchema,
  makeCursorState,
} from "./tools.js";

const SERVER_NAME = "live";
// `../..` resolves the package root from both `src/mcp/` (tsx dev) and
// `dist/mcp/` (built).
const PKG_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SERVER_VERSION = (
  createRequire(import.meta.url)("../../package.json") as { version: string }
).version;

const SERVER_INSTRUCTIONS = `\`live\` records terminal sessions to disk. Use \`list_sessions\` to discover sessions. For ad-hoc reads, run shell tools (\`cat\`, \`tail\`, \`grep\`) on \`<path>/stream.*.log\` directly. For resumable polling on new content, call \`cursor\` — it tracks your position per-conversation and returns just the new lines on each call.

For deeper detail, fetch the resources \`live://docs/readme\` (install, config, usage) or \`live://docs/design\` (architecture, on-disk layout, edge cases).`;

const LIST_SESSIONS_DESCRIPTION = `List sessions from every \`.live/\` directory under the current working directory. Live sessions only by default; pass \`include_exited: true\` for exited ones too.

Entries are returned newest-first (ULID-sorted descending), so the first entry is the most recent. Pass \`name\` to filter to sessions launched with \`live --name <value>\` — the first match is the most recent, useful when a name has been reused.

Each entry has a \`path\` — read \`<path>/stream.*.log\` directly with shell tools:
  - \`cat <path>/stream.*.log | tail -n 200\` — recent output
  - \`grep ERROR <path>/stream.*.log\` — scan for errors
  - per-line timestamps in \`<path>/lines.*.log\` as JSONL \`{n, t}\`

For polling on new content, use \`cursor(path, session_id)\`.

\`consistent: false\` means the writer was killed mid-record-write; one trailing stream line has no \`{n, t}\` record (visible in raw reads, not tracked by \`cursor\`).`;

const CURSOR_DESCRIPTION = `Returns the segments and skip count to read new lines from a session. The server tracks your position per \`(path, session_id)\` for this MCP connection's lifetime.

Read the result with:
  \`cat <path>/{segments…} 2>/dev/null | tail -n +$((skip_lines + 1))\`
  (prepend \`<path>/\` to each filename in \`segments\`)

First call places the cursor at current \`lastLine\` and returns \`segments: []\` (backlog skipped). For backlog, read \`<path>/stream.*.log\` directly first.

\`segments: []\` on later calls: nothing new — poll again.

\`gap: true\`: retention dropped tracked lines. \`segments\` is everything still on disk; the range below current \`firstLine\` is unrecoverable.

Optional \`since_line\` overrides the tracked cursor — useful to backfill, replay, or recover from a gap.`;

/**
 * Build and start the MCP server on stdio. Cursor state is per-process
 * (= per MCP connection).
 */
export async function startMcpServer(): Promise<void> {
  const server = new McpServer(
    { name: SERVER_NAME, version: SERVER_VERSION },
    { instructions: SERVER_INSTRUCTIONS },
  );

  const cursorState = makeCursorState();
  // Scan once per MCP connection. `.live/` directories created after the
  // connection started don't show up until the next start.
  const liveDirs = await findLiveDirs(process.cwd());

  server.registerTool(
    "list_sessions",
    {
      description: LIST_SESSIONS_DESCRIPTION,
      inputSchema: listSessionsInputSchema,
      outputSchema: listSessionsOutputSchema,
    },
    async (args) => {
      const sessions = await listSessions(liveDirs, args);
      const structuredContent = { sessions };
      return {
        content: [
          { type: "text" as const, text: JSON.stringify(structuredContent, null, 2) },
        ],
        structuredContent,
      };
    },
  );

  server.registerTool(
    "cursor",
    {
      description: CURSOR_DESCRIPTION,
      inputSchema: cursorInputSchema,
      outputSchema: cursorOutputSchema,
    },
    async (args) => {
      const structuredContent = await cursor(cursorState, args);
      return {
        content: [
          { type: "text" as const, text: JSON.stringify(structuredContent, null, 2) },
        ],
        structuredContent,
      };
    },
  );

  server.registerResource(
    "readme",
    "live://docs/readme",
    {
      title: "live — README",
      description:
        "Install, usage, MCP client config, shell completion, per-project config.",
      mimeType: "text/markdown",
    },
    async (uri) => ({
      contents: [
        {
          uri: uri.href,
          mimeType: "text/markdown",
          text: await readFile(join(PKG_ROOT, "README.md"), "utf8"),
        },
      ],
    }),
  );

  server.registerResource(
    "design",
    "live://docs/design",
    {
      title: "live — DESIGN",
      description:
        "Architecture, on-disk layout, recording invariants, segments + retention, liveness, MCP tool semantics and edge cases.",
      mimeType: "text/markdown",
    },
    async (uri) => ({
      contents: [
        {
          uri: uri.href,
          mimeType: "text/markdown",
          text: await readFile(join(PKG_ROOT, "DESIGN.md"), "utf8"),
        },
      ],
    }),
  );

  const transport = new StdioServerTransport();
  await server.connect(transport);
}
