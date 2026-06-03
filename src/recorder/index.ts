import { mkdir, open, rm, unlink } from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import { spawn, type IPty } from "node-pty";
import { ulid } from "ulid";
import { ensureLiveDir, findAnchor } from "../session/discovery.js";
import { sweep, ensureDeadAt } from "../session/lifecycle.js";
import { writeMeta, type Meta } from "../session/meta.js";
import { linesPath, sizeOf, streamPath } from "../session/segments.js";
import { DEFAULT_CONFIG, loadConfigFor } from "../util/config.js";
import { acquireExclusive } from "../util/lock.js";
import { processLockPath, sessionDir } from "../util/paths.js";
import { LineWriter } from "./lineWriter.js";

export interface RecorderOptions {
  cwd: string;
  command: string[];
  /** Optional user-supplied label exposed via `list_sessions`. */
  name?: string;
}

/**
 * Run `command` under a PTY, mirror its output to stdout, and record to
 * segmented logs under the nearest `.live/` walking up from `cwd` (or
 * `~/.live/`). Returns the child exit code, or 128 + signal if killed.
 *
 * Signals aren't trapped: the PTY delivers them to the child, and our flock
 * releases on process exit. A sweeper then stamps the consistency verdict.
 */
export async function run(opts: RecorderOptions): Promise<number> {
  const { cwd, command, name } = opts;
  if (command.length === 0) {
    throw new Error("live: no command given");
  }
  const [program, ...args] = command;
  if (program === undefined) {
    throw new Error("live: no command given");
  }

  const liveDir = await findAnchor(cwd);
  await ensureLiveDir(liveDir);

  // Tolerate a broken config so the user's command still runs; the MCP
  // server surfaces the same failure as an error to the agent.
  let config;
  try {
    config = await loadConfigFor(liveDir);
  } catch (err) {
    console.warn(
      `[live] config load failed, using defaults: ${(err as Error).message}`,
    );
    config = DEFAULT_CONFIG;
  }

  await sweep(liveDir, config.ttlDays).catch((err) => {
    console.warn("[live] startup sweep failed:", (err as Error).message);
  });

  const sessionId = ulid();
  const sessDir = sessionDir(liveDir, sessionId);
  await mkdir(sessDir, { recursive: true });

  // Roll back sessDir on flock acquisition failure so we don't leak it.
  let lockFh: FileHandle;
  try {
    lockFh = await open(processLockPath(sessDir), "w");
  } catch (err) {
    await rm(sessDir, { recursive: true, force: true });
    throw err;
  }
  try {
    await acquireExclusive(lockFh.fd);
  } catch (err) {
    await lockFh.close().catch(() => {});
    await rm(sessDir, { recursive: true, force: true });
    throw err;
  }

  const startedAt = Date.now();
  const meta: Meta = {
    id: sessionId,
    command,
    cwd,
    ...(name !== undefined ? { name } : {}),
    startedAt,
    exitedAt: null,
    status: "running",
    exitCode: null,
    firstSegment: 0,
    lastSegment: 0,
  };
  const segmentByteLimit = config.segmentKb * 1024;
  const retentionByteLimit = config.maxKb * 1024;

  // Any failure before pty.spawn must release the flock and remove sessDir.
  let initialStream: FileHandle | undefined;
  let initialLines: FileHandle | undefined;
  let lineWriter!: LineWriter;
  try {
    await writeMeta(sessDir, meta);
    initialStream = await open(streamPath(sessDir, meta.lastSegment), "a");
    initialLines = await open(linesPath(sessDir, meta.lastSegment), "a");
    lineWriter = new LineWriter(
      { streamFh: initialStream, linesFh: initialLines },
      {
        segmentByteLimit,
        // Bump `lastSegment` BEFORE opening the new pair: readers tolerate a
        // not-yet-existing segment, but new files outside meta's range would
        // be invisible until TTL.
        onRotate: async () => {
          meta.lastSegment += 1;
          await writeMeta(sessDir, meta);
          const streamFh = await open(streamPath(sessDir, meta.lastSegment), "a");
          const linesFh = await open(linesPath(sessDir, meta.lastSegment), "a");
          await retain();
          return { streamFh, linesFh };
        },
      },
    );
  } catch (err) {
    await initialStream?.close().catch(() => {});
    await initialLines?.close().catch(() => {});
    await lockFh.close().catch(() => {});
    await rm(sessDir, { recursive: true, force: true });
    throw err;
  }

  const stdinIsTty = process.stdin.isTTY === true;
  const stdoutIsTty = process.stdout.isTTY === true;
  const cols = stdoutIsTty ? (process.stdout.columns ?? 80) : 80;
  const rows = stdoutIsTty ? (process.stdout.rows ?? 24) : 24;

  let pty: IPty;
  try {
    pty = spawn(program, args, {
      name: process.env["TERM"] ?? "xterm-256color",
      cols,
      rows,
      cwd,
      env: process.env as { [key: string]: string },
    });
  } catch (err) {
    await lockFh.close();
    await lineWriter.close();
    await rm(sessDir, { recursive: true, force: true });
    throw err;
  }

  if (stdinIsTty) {
    process.stdin.setRawMode(true);
  }
  process.stdin.resume();

  const stdinListener = (chunk: Buffer): void => {
    pty.write(chunk.toString("utf8"));
  };
  process.stdin.on("data", stdinListener);

  const sigwinchListener = (): void => {
    if (stdoutIsTty) {
      pty.resize(process.stdout.columns ?? 80, process.stdout.rows ?? 24);
    }
  };
  process.on("SIGWINCH", sigwinchListener);

  // Serialize disk writes via a promise chain so per-line ordering is
  // preserved. On first write error we kill the PTY: the user has already
  // seen the mirrored output, so swallowing the error would leave the
  // recording silently diverged from the terminal.
  let writeError: Error | null = null;
  let writeChain: Promise<void> = Promise.resolve();
  pty.onData((data: string) => {
    process.stdout.write(data);
    if (writeError !== null) return;
    writeChain = writeChain.then(() => lineWriter.processChunk(data)).catch((err) => {
      if (writeError !== null) return;
      writeError = err as Error;
      console.error(
        `[live] recording failed (${writeError.message}); terminating child to avoid silent loss`,
      );
      try {
        pty.kill();
      } catch {
        // Already dead.
      }
    });
  });

  async function retain(): Promise<void> {
    let total = 0;
    for (let n = meta.firstSegment; n <= meta.lastSegment; n++) {
      total += await sizeOf(streamPath(sessDir, n));
    }
    while (total > retentionByteLimit && meta.firstSegment < meta.lastSegment) {
      const dropN = meta.firstSegment;
      const dropStream = streamPath(sessDir, dropN);
      const dropLines = linesPath(sessDir, dropN);
      const dropSize = await sizeOf(dropStream);
      await unlinkIgnoreEnoent(dropStream);
      await unlinkIgnoreEnoent(dropLines);
      meta.firstSegment += 1;
      total -= dropSize;
    }
    await writeMeta(sessDir, meta);
  }

  // Stamp exitedAt from onExit so it reflects the exit moment, not cleanup.
  let exitedAtMs = 0;
  const exitCode: number = await new Promise<number>((resolve) => {
    pty.onExit(({ exitCode, signal }) => {
      exitedAtMs = Date.now();
      // node-pty reports exitCode=0 for signal-killed children; surface
      // 128+signal like a shell.
      if (signal && exitCode === 0) resolve(128 + signal);
      else resolve(exitCode);
    });
  });

  // Drain to fixpoint: trailing onData reassigns writeChain, so a single
  // await on a snapshot may miss the last writes.
  for (;;) {
    const snapshot = writeChain;
    await snapshot;
    if (snapshot === writeChain) break;
  }
  process.stdin.off("data", stdinListener);
  process.off("SIGWINCH", sigwinchListener);
  if (stdinIsTty) {
    process.stdin.setRawMode(false);
  }
  process.stdin.pause();

  await lineWriter.close();

  meta.status = "exited";
  meta.exitCode = exitCode;
  meta.exitedAt = exitedAtMs;
  // Both writes are best-effort so a failure on one (e.g. disk full) can't
  // block the other — sweepers rely on deadAt to decide retention.
  try {
    await writeMeta(sessDir, meta);
  } catch (err) {
    console.warn(`[live] final meta write failed: ${(err as Error).message}`);
  }

  // Pass the verdict explicitly so ensureDeadAt skips the consistency check:
  // the final {n,t} was written before flock release, so a graceful exit is
  // consistent by construction. A mid-session write error marks inconsistent.
  try {
    await ensureDeadAt(sessDir, writeError === null ? "consistent" : "inconsistent");
  } catch (err) {
    console.warn(`[live] deadAt stamp failed: ${(err as Error).message}`);
  }

  await lockFh.close();
  return exitCode;
}

async function unlinkIgnoreEnoent(path: string): Promise<void> {
  try {
    await unlink(path);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return;
    throw err;
  }
}

