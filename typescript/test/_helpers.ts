import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Meta } from "../src/session/meta.js";

/**
 * Make a fresh tempdir for an isolated test. Caller registers cleanup with
 * `t.after(() => cleanup(dir))`.
 *
 * Returns the canonical (realpath'd) path so tests can compare against
 * results from code paths that realpath internally (notably
 * `resolveIncludes`) — without this, macOS's `/var → /private/var` symlink
 * makes string-compare assertions fail.
 */
export async function mkTmp(prefix = "live-test-"): Promise<string> {
  return realpath(await mkdtemp(join(tmpdir(), prefix)));
}

export async function cleanup(path: string): Promise<void> {
  await rm(path, { recursive: true, force: true });
}

/**
 * Build a synthetic session directory with the given segments and meta.
 * `segments` is keyed by segment number; each entry's `lines` is an array of
 * `{n, t}` JSONL records and `stream` is the matching line content.
 */
export interface SyntheticSegment {
  lines: { n: number; t: number }[];
  /** One string per line; newline appended automatically. Default: derived from `lines`. */
  stream?: string[];
}

export async function makeSession(
  sessDir: string,
  opts: {
    id?: string;
    command?: string[];
    cwd?: string;
    name?: string;
    startedAt?: number;
    exitedAt?: number | null;
    status?: "running" | "exited";
    exitCode?: number | null;
    firstSegment?: number;
    lastSegment?: number;
    segments?: Record<number, SyntheticSegment>;
  } = {},
): Promise<Meta> {
  await mkdir(sessDir, { recursive: true });
  const segments = opts.segments ?? {};
  const segNums = Object.keys(segments).map(Number).sort((a, b) => a - b);
  const firstSegment = opts.firstSegment ?? (segNums[0] ?? 0);
  const lastSegment = opts.lastSegment ?? (segNums[segNums.length - 1] ?? 0);
  const meta: Meta = {
    id: opts.id ?? "01TESTSESSIONULIDABCDEFGH",
    command: opts.command ?? ["echo", "hi"],
    cwd: opts.cwd ?? sessDir,
    ...(opts.name !== undefined ? { name: opts.name } : {}),
    startedAt: opts.startedAt ?? 1_700_000_000_000,
    exitedAt: opts.exitedAt ?? null,
    status: opts.status ?? "running",
    exitCode: opts.exitCode ?? null,
    firstSegment,
    lastSegment,
  };
  await writeFile(
    join(sessDir, "meta.json"),
    JSON.stringify(meta, null, 2) + "\n",
  );
  for (const [n, seg] of Object.entries(segments)) {
    const segN = Number(n);
    const linesPath = join(sessDir, `lines.${pad4(segN)}.log`);
    const streamPath = join(sessDir, `stream.${pad4(segN)}.log`);
    const linesContent = seg.lines.map((r) => JSON.stringify(r) + "\n").join("");
    const streamLines = seg.stream ?? seg.lines.map((r) => `line ${r.n}`);
    const streamContent = streamLines.map((s) => s + "\n").join("");
    await writeFile(linesPath, linesContent);
    await writeFile(streamPath, streamContent);
  }
  return meta;
}

function pad4(n: number): string {
  return n.toString().padStart(4, "0");
}
