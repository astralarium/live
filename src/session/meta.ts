import { readFile } from "node:fs/promises";
import { atomicWriteFile } from "../util/atomic.js";
import { metaPath } from "../util/paths.js";
import {
  linesPath,
  readFirstRecord,
  readLastCompleteRecord,
} from "./segments.js";

export interface Meta {
  id: string;
  command: string[];
  cwd: string;
  /** User-supplied label from `live --name`. Absent if unset. */
  name?: string;
  startedAt: number;
  exitedAt: number | null;
  status: "running" | "exited";
  exitCode: number | null;
  firstSegment: number;
  lastSegment: number;
}

export interface DerivedLines {
  firstLine: number;
  lastLine: number;
  count: number;
  /**
   * Segment containing `lastLine`. Lower than `meta.lastSegment` when the
   * current segment just rotated and has no records; `0` for an empty
   * session.
   */
  lastLineSegment: number;
}

/** Read and shape-validate `meta.json`. Throws on ENOENT or malformed. */
export async function readMeta(sessDir: string): Promise<Meta> {
  const raw = await readFile(metaPath(sessDir), "utf8");
  const parsed: unknown = JSON.parse(raw);
  if (!isValidMeta(parsed)) {
    throw new Error(`malformed meta.json at ${metaPath(sessDir)}`);
  }
  return parsed;
}

/** Shape check covering only the fields downstream code indexes. */
function isValidMeta(value: unknown): value is Meta {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v["id"] === "string" &&
    Array.isArray(v["command"]) &&
    typeof v["cwd"] === "string" &&
    (v["name"] === undefined || typeof v["name"] === "string") &&
    typeof v["startedAt"] === "number" &&
    (v["exitedAt"] === null || typeof v["exitedAt"] === "number") &&
    (v["status"] === "running" || v["status"] === "exited") &&
    (v["exitCode"] === null || typeof v["exitCode"] === "number") &&
    typeof v["firstSegment"] === "number" &&
    typeof v["lastSegment"] === "number"
  );
}

/** Like `readMeta` but returns `null` when the file is absent. */
export async function tryReadMeta(sessDir: string): Promise<Meta | null> {
  try {
    return await readMeta(sessDir);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

/** Atomic meta.json write — readers never observe a partial file. */
export async function writeMeta(sessDir: string, meta: Meta): Promise<void> {
  await atomicWriteFile(metaPath(sessDir), JSON.stringify(meta, null, 2) + "\n");
}

/**
 * Derive line watermarks from disk. Brand-new and just-rotated cases walk
 * back to the last segment with records; truncated trailing records are
 * tolerated.
 */
export async function deriveLines(
  sessDir: string,
  meta: Meta,
): Promise<DerivedLines> {
  const first = await readFirstRecord(linesPath(sessDir, meta.firstSegment));
  if (first === null) {
    return { firstLine: 1, lastLine: 0, count: 0, lastLineSegment: 0 };
  }
  const firstLine = first.n;

  let lastLine = 0;
  let lastLineSegment = 0;
  for (let n = meta.lastSegment; n >= meta.firstSegment; n--) {
    const last = await readLastCompleteRecord(linesPath(sessDir, n));
    if (last !== null) {
      lastLine = last.n;
      lastLineSegment = n;
      break;
    }
  }

  // Defensive: retention could race the walk-back.
  if (lastLine === 0) {
    return { firstLine: 1, lastLine: 0, count: 0, lastLineSegment: 0 };
  }

  return {
    firstLine,
    lastLine,
    count: lastLine - firstLine + 1,
    lastLineSegment,
  };
}
