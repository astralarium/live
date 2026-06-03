import { readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { z } from "zod";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
import {
  isAlive,
  readDeadAtTime,
  readDeadAtVerdict,
  sweep,
} from "../session/lifecycle.js";
import { deriveLines, tryReadMeta, type Meta } from "../session/meta.js";
import { linesPath, readFirstRecord, streamName } from "../session/segments.js";
import { loadConfigFor, type Config } from "../util/config.js";
import { CONFIG_FILENAME, sessionsDir } from "../util/paths.js";

// ---------- list_sessions ----------

export const listSessionsInputSchema = {
  include_exited: z
    .boolean()
    .optional()
    .describe(
      "If true, include sessions whose `live` process has ended. Default false (live sessions only).",
    ),
  name: z
    .string()
    .optional()
    .describe(
      "If set, only sessions started with `live --name <value>` matching this string. Entries are ULID-sorted with most recent sessions first.",
    ),
};

const sessionEntrySchema = z.object({
  id: z.string(),
  name: z.string().optional(),
  command: z.array(z.string()),
  cwd: z.string(),
  status: z.enum(["running", "exited"]),
  consistent: z.boolean(),
  startedAt: z.number(),
  exitedAt: z.number().optional(),
  exitCode: z.number().optional(),
  path: z.string(),
  firstSegment: z.number().int(),
  lastSegment: z.number().int(),
  firstLine: z.number().int(),
  lastLine: z.number().int(),
  count: z.number().int(),
});

export const listSessionsOutputSchema = {
  sessions: z.array(sessionEntrySchema),
};

export type SessionEntry = z.infer<typeof sessionEntrySchema>;

export async function listSessions(
  liveDirs: readonly string[],
  args: { include_exited?: boolean; name?: string },
): Promise<SessionEntry[]> {
  const entries: SessionEntry[] = [];
  for (const liveDir of liveDirs) {
    // Each `.live/` is swept under its own effective config; surface load
    // errors so the agent doesn't silently inherit defaults.
    let config: Config;
    try {
      config = await loadConfigFor(liveDir);
    } catch (err) {
      throw new McpError(
        ErrorCode.InternalError,
        `failed to load config for ${liveDir} (check ~/.live/${CONFIG_FILENAME} and ${liveDir}/${CONFIG_FILENAME}): ${(err as Error).message}`,
      );
    }
    await sweep(liveDir, config.ttlDays).catch((err) => {
      console.error("[live] sweep failed:", (err as Error).message);
    });
    let sessionIds: string[];
    try {
      sessionIds = await readdir(sessionsDir(liveDir));
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "ENOENT") continue;
      throw err;
    }
    for (const id of sessionIds) {
      const sessDir = join(sessionsDir(liveDir), id);
      const entry = await buildEntry(sessDir, id, args.include_exited ?? false);
      if (!entry) continue;
      if (args.name !== undefined && entry.name !== args.name) continue;
      entries.push(entry);
    }
  }
  // ULID lex desc = chronological desc; most recent first.
  entries.sort((a, b) => b.id.localeCompare(a.id));
  return entries;
}

async function buildEntry(
  sessDir: string,
  id: string,
  includeExited: boolean,
): Promise<SessionEntry | null> {
  const meta = await tryReadMeta(sessDir);
  if (!meta) return null; // half-created or already-removed
  const alive = await isAlive(sessDir);
  if (!alive && !includeExited) return null;

  let consistent = true;
  if (!alive) {
    const verdict = await readDeadAtVerdict(sessDir);
    if (verdict === "inconsistent") consistent = false;
  }

  // Graceful exits stamp meta.exitedAt; crashes leave it null and fall back
  // to deadAt's mtime.
  let exitedAt: number | undefined;
  if (meta.exitedAt !== null) {
    exitedAt = meta.exitedAt;
  } else if (!alive) {
    const t = await readDeadAtTime(sessDir);
    if (t !== null) exitedAt = t;
  }

  const { firstLine, lastLine, count } = await deriveLines(sessDir, meta);

  return {
    id,
    ...(meta.name !== undefined ? { name: meta.name } : {}),
    command: meta.command,
    cwd: meta.cwd,
    status: alive ? "running" : "exited",
    consistent,
    startedAt: meta.startedAt,
    ...(exitedAt !== undefined ? { exitedAt } : {}),
    ...(meta.exitCode !== null ? { exitCode: meta.exitCode } : {}),
    path: sessDir,
    firstSegment: meta.firstSegment,
    lastSegment: meta.lastSegment,
    firstLine,
    lastLine,
    count,
  };
}

// ---------- cursor ----------

export const cursorInputSchema = {
  path: z.string().describe("Absolute path to the session directory, from list_sessions."),
  session_id: z
    .string()
    .describe(
      "ULID from list_sessions. Verified against the session's meta.json; the call fails if they disagree.",
    ),
  since_line: z
    .number()
    .int()
    .optional()
    .describe(
      "Optional override. When omitted, the server uses its tracked cursor for this (path, session_id). When provided, reads from that line forward and updates the tracked cursor to match.",
    ),
};

export const cursorOutputSchema = {
  segments: z.array(z.string()),
  skip_lines: z.number().int(),
  last_line: z.number().int(),
  gap: z.boolean(),
};

export type CursorResult = {
  segments: string[];
  skip_lines: number;
  last_line: number;
  gap: boolean;
};

/**
 * Per-MCP-connection state. `cursors` tracks each session's last-returned
 * line keyed by `${path}|${session_id}`; `sweepCooldowns` rate-limits the
 * opportunistic sweep `cursor` fires per `.live/`.
 */
export interface CursorState {
  cursors: Map<string, number>;
  sweepCooldowns: Map<string, number>;
}

export function makeCursorState(): CursorState {
  return { cursors: new Map(), sweepCooldowns: new Map() };
}

/**
 * Advance the cursor for `(path, session_id)` and return segments + skip
 * for the new range. Agent-facing contract: see `CURSOR_DESCRIPTION` in
 * `server.ts`.
 */
export async function cursor(
  state: CursorState,
  args: { path: string; session_id: string; since_line?: number },
): Promise<CursorResult> {
  const { path: sessDir, session_id } = args;
  const meta = await tryReadMeta(sessDir);
  if (!meta) {
    throw new McpError(
      ErrorCode.InvalidParams,
      `session not found at ${sessDir} (was TTL-cleaned or path is wrong); call list_sessions to re-discover`,
    );
  }
  if (meta.id !== session_id) {
    throw new McpError(
      ErrorCode.InvalidParams,
      `session_id mismatch at ${sessDir}: expected ${meta.id}, got ${session_id}`,
    );
  }

  // Sweep the owning `.live/` opportunistically so polling-only agents see
  // neighbor deaths. sessDir is `<liveDir>/sessions/<ulid>`.
  void maybeSweep(state.sweepCooldowns, dirname(dirname(sessDir)));

  const { firstLine, lastLine, lastLineSegment } = await deriveLines(sessDir, meta);
  const stateKey = `${sessDir}|${session_id}`;

  let effective: number;
  if (args.since_line !== undefined) {
    effective = args.since_line;
  } else if (state.cursors.has(stateKey)) {
    effective = state.cursors.get(stateKey)!;
  } else {
    // First call: anchor at current `lastLine` and skip the backlog (the
    // agent reads it directly with shell tools if it wants it).
    state.cursors.set(stateKey, lastLine);
    return {
      segments: [],
      skip_lines: 0,
      last_line: lastLine,
      gap: false,
    };
  }

  if (lastLine === 0) {
    state.cursors.set(stateKey, 0);
    return { segments: [], skip_lines: 0, last_line: 0, gap: false };
  }

  if (effective < firstLine) {
    const segs = await collectSegmentsWithRecords(sessDir, meta);
    state.cursors.set(stateKey, lastLine);
    return {
      segments: segs,
      skip_lines: 0,
      last_line: lastLine,
      gap: true,
    };
  }

  if (effective >= lastLine) {
    state.cursors.set(stateKey, lastLine);
    return { segments: [], skip_lines: 0, last_line: lastLine, gap: false };
  }

  // Find the segment containing line `effective + 1`. Boundary case:
  // when `first.n == effective + 1` that segment itself is the answer, so
  // the test is `> effective + 1`, not `> effective`.
  let kMinusOne = lastLineSegment;
  let firstNOfKMinusOne: number | null = null;
  for (let n = meta.firstSegment; n <= lastLineSegment; n++) {
    const first = await readFirstRecord(linesPath(sessDir, n));
    if (first === null) continue; // just-rotated empty segment
    if (first.n > effective + 1) {
      kMinusOne = n - 1;
      break;
    }
    kMinusOne = n;
    firstNOfKMinusOne = first.n;
  }
  if (firstNOfKMinusOne === null) {
    // Unreachable given the caught-up guard above.
    const first = await readFirstRecord(linesPath(sessDir, kMinusOne));
    if (first === null) {
      throw new McpError(
        ErrorCode.InternalError,
        `cursor invariant violated at ${sessDir}: forward-scan found no records but lastLine=${lastLine}`,
      );
    }
    firstNOfKMinusOne = first.n;
  }

  const skipLines = effective - firstNOfKMinusOne + 1;
  // Bound at lastLineSegment so a just-rotated empty current segment is
  // excluded.
  const segs = collectSegmentsRange(kMinusOne, lastLineSegment);

  state.cursors.set(stateKey, lastLine);
  return {
    segments: segs,
    skip_lines: skipLines,
    last_line: lastLine,
    gap: false,
  };
}

function collectSegmentsRange(from: number, to: number): string[] {
  const out: string[] = [];
  for (let n = from; n <= to; n++) {
    out.push(streamName(n));
  }
  return out;
}

/** Every retained segment with at least one complete record. */
async function collectSegmentsWithRecords(
  sessDir: string,
  meta: Meta,
): Promise<string[]> {
  const out: string[] = [];
  for (let n = meta.firstSegment; n <= meta.lastSegment; n++) {
    const first = await readFirstRecord(linesPath(sessDir, n));
    if (first !== null) out.push(streamName(n));
  }
  return out;
}

// ---------- opportunistic sweep from cursor ----------

const SWEEP_INTERVAL_MS = 60_000;

/** Sweep `liveDir` at most once per `SWEEP_INTERVAL_MS` per connection. */
async function maybeSweep(
  cooldowns: Map<string, number>,
  liveDir: string,
): Promise<void> {
  const now = Date.now();
  const last = cooldowns.get(liveDir);
  if (last !== undefined && now - last < SWEEP_INTERVAL_MS) return;
  // Set cooldown before awaiting so concurrent callers don't all race past.
  cooldowns.set(liveDir, now);
  let ttlDays: number;
  try {
    ttlDays = (await loadConfigFor(liveDir)).ttlDays;
  } catch (err) {
    // Don't break polling on a bad config; list_sessions surfaces the error.
    console.warn(
      `[live] cursor sweep config load failed for ${liveDir}: ${(err as Error).message}`,
    );
    return;
  }
  await sweep(liveDir, ttlDays).catch((err) => {
    console.error("[live] cursor sweep failed:", (err as Error).message);
  });
}
