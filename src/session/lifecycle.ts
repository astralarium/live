import { open, readdir, readFile, rm, stat } from "node:fs/promises";
import { join } from "node:path";
import { tryAcquireExclusive } from "../util/lock.js";
import {
  deadAtPath,
  processLockPath,
  sessionsDir,
} from "../util/paths.js";
import { tryReadMeta } from "./meta.js";
import { countCompleteRecords, linesPath, streamPath } from "./segments.js";

const MS_PER_DAY = 86_400_000;

export type Consistency = "consistent" | "inconsistent";

/**
 * Probe liveness by trying to acquire `process.lock` non-blocking. Acquired
 * → holder is gone (released by closing the fd in `finally`). EAGAIN/
 * EWOULDBLOCK → live writer still holds it.
 */
export async function isAlive(sessDir: string): Promise<boolean> {
  let fh;
  try {
    fh = await open(processLockPath(sessDir), "r");
    const acquired = await tryAcquireExclusive(fh.fd);
    return !acquired;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw err;
  } finally {
    await fh?.close();
  }
}

/**
 * Sweep one `.live/`: stamp `deadAt` (with consistency verdict) on dead
 * sessions, then delete those whose `deadAt` mtime is older than `ttlDays`.
 */
export async function sweep(liveDir: string, ttlDays: number): Promise<void> {
  const ttlMs = ttlDays * MS_PER_DAY;
  const dir = sessionsDir(liveDir);
  let entries: string[];
  try {
    entries = await readdir(dir);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return;
    throw err;
  }
  for (const id of entries) {
    const sessDir = join(dir, id);
    try {
      if (await isAlive(sessDir)) continue;
      await ensureDeadAt(sessDir);
      const deadStat = await stat(deadAtPath(sessDir));
      const age = Date.now() - deadStat.mtimeMs;
      if (age > ttlMs) {
        await rm(sessDir, { recursive: true, force: true });
      }
    } catch (err) {
      // Benign race: a parallel sweeper's TTL `rm` removed the session dir
      // while we were stat'ing it. ensureDeadAt swallows its own EEXIST.
      if ((err as NodeJS.ErrnoException).code === "ENOENT") continue;
      console.warn(`[live] sweep error on ${sessDir}:`, (err as Error).message);
    }
  }
}

/**
 * Stamp `deadAt` with the consistency verdict if it doesn't exist (O_EXCL).
 * Empty content = `"consistent"`, `"inconsistent\n"` otherwise. Pass `verdict`
 * to skip the streaming check; sweepers omit it and pay for the check.
 */
export async function ensureDeadAt(
  sessDir: string,
  verdict?: Consistency,
): Promise<void> {
  const consistency = verdict ?? (await checkConsistency(sessDir));
  const content = consistency === "consistent" ? "" : "inconsistent\n";
  let fh;
  try {
    fh = await open(deadAtPath(sessDir), "wx");
    await fh.writeFile(content);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EEXIST") return;
    throw err;
  } finally {
    await fh?.close();
  }
}

/** Read `deadAt`: empty → consistent, non-empty → inconsistent, ENOENT → null. */
export async function readDeadAtVerdict(
  sessDir: string,
): Promise<Consistency | null> {
  try {
    const raw = await readFile(deadAtPath(sessDir), "utf8");
    return raw.trim().length === 0 ? "consistent" : "inconsistent";
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

/** Return `deadAt`'s mtime in ms epoch, or null if absent. */
export async function readDeadAtTime(sessDir: string): Promise<number | null> {
  try {
    const s = await stat(deadAtPath(sessDir));
    return s.mtimeMs;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

/**
 * Compare stream-line count vs lines-record count in the last segment.
 * Equal → consistent; stream one ahead → inconsistent. Any other diff
 * violates the write-order invariant; logged but still reported inconsistent.
 */
async function checkConsistency(sessDir: string): Promise<Consistency> {
  const meta = await tryReadMeta(sessDir);
  if (!meta) return "consistent";
  const streamLines = await countCompleteRecords(streamPath(sessDir, meta.lastSegment));
  const linesRecords = await countCompleteRecords(linesPath(sessDir, meta.lastSegment));
  const diff = streamLines - linesRecords;
  if (diff === 0) return "consistent";
  if (diff !== 1) {
    console.warn(
      `[live] unexpected consistency diff at ${sessDir} segment ${meta.lastSegment}: stream=${streamLines} lines=${linesRecords} (expected diff 0 or 1)`,
    );
  }
  return "inconsistent";
}
