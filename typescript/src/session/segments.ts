import { open, stat } from "node:fs/promises";
import { join } from "node:path";

const SEGMENT_DIGITS = 4;
const STREAM_PREFIX = "stream.";
const LINES_PREFIX = "lines.";
const SEGMENT_SUFFIX = ".log";

export interface LineRecord {
  n: number;
  t: number;
}

function formatSegment(n: number): string {
  return n.toString().padStart(SEGMENT_DIGITS, "0");
}

export function streamName(n: number): string {
  return `${STREAM_PREFIX}${formatSegment(n)}${SEGMENT_SUFFIX}`;
}

function linesName(n: number): string {
  return `${LINES_PREFIX}${formatSegment(n)}${SEGMENT_SUFFIX}`;
}

export function streamPath(sessDir: string, n: number): string {
  return join(sessDir, streamName(n));
}

export function linesPath(sessDir: string, n: number): string {
  return join(sessDir, linesName(n));
}

/** First complete JSONL record from `lines.NNNN.log`, or null. */
export async function readFirstRecord(path: string): Promise<LineRecord | null> {
  let fh;
  try {
    fh = await open(path, "r");
    const stats = await fh.stat();
    if (stats.size === 0) return null;
    const buf = Buffer.alloc(Math.min(stats.size, 4096));
    await fh.read(buf, 0, buf.length, 0);
    const newline = buf.indexOf(0x0a);
    if (newline < 0) return null;
    const line = buf.subarray(0, newline).toString("utf8");
    return parseRecord(line);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  } finally {
    await fh?.close();
  }
}

/**
 * Last complete JSONL record from `lines.NNNN.log`, or null. Tolerates a
 * truncated trailing record by scanning back to the last `\n`.
 */
export async function readLastCompleteRecord(
  path: string,
): Promise<LineRecord | null> {
  let fh;
  try {
    fh = await open(path, "r");
    const stats = await fh.stat();
    if (stats.size === 0) return null;
    const tailLen = Math.min(stats.size, 8192);
    const buf = Buffer.alloc(tailLen);
    await fh.read(buf, 0, tailLen, stats.size - tailLen);
    const lastNL = buf.lastIndexOf(0x0a);
    if (lastNL < 0) return null;
    const prevNL = buf.lastIndexOf(0x0a, lastNL - 1);
    const start = prevNL < 0 ? 0 : prevNL + 1;
    const line = buf.subarray(start, lastNL).toString("utf8");
    return parseRecord(line);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  } finally {
    await fh?.close();
  }
}

/** Count newline-terminated records, ignoring any truncated trailing line. */
export async function countCompleteRecords(path: string): Promise<number> {
  let fh;
  try {
    fh = await open(path, "r");
    const stats = await fh.stat();
    if (stats.size === 0) return 0;
    const buf = Buffer.alloc(stats.size);
    await fh.read(buf, 0, stats.size, 0);
    let count = 0;
    for (let i = 0; i < buf.length; i++) {
      if (buf[i] === 0x0a) count++;
    }
    return count;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return 0;
    throw err;
  } finally {
    await fh?.close();
  }
}

/** Byte size of `path`, or 0 if missing. */
export async function sizeOf(path: string): Promise<number> {
  try {
    const s = await stat(path);
    return s.size;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return 0;
    throw err;
  }
}

function parseRecord(line: string): LineRecord | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    const obj = JSON.parse(trimmed) as unknown;
    if (
      obj &&
      typeof obj === "object" &&
      "n" in obj &&
      "t" in obj &&
      typeof (obj as LineRecord).n === "number" &&
      typeof (obj as LineRecord).t === "number"
    ) {
      return obj as LineRecord;
    }
    return null;
  } catch {
    return null;
  }
}
