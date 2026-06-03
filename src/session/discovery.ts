import { mkdir, readdir, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import {
  HOME_LIVE_DIR,
  LIVE_DIRNAME,
  gitignorePath,
  sessionsDir,
} from "../util/paths.js";

/** Walk up from `cwd` to the nearest `.live/`; falls back to `~/.live/`. */
export async function findAnchor(cwd: string): Promise<string> {
  let dir = cwd;
  while (true) {
    const candidate = join(dir, LIVE_DIRNAME);
    if (await isDirectory(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break; // filesystem root
    dir = parent;
  }
  await ensureLiveDir(HOME_LIVE_DIR);
  return HOME_LIVE_DIR;
}

/** Idempotently create a `.live/` and its `sessions/` subdir. */
export async function ensureLiveDir(liveDir: string): Promise<void> {
  await mkdir(sessionsDir(liveDir), { recursive: true });
}

/**
 * Explicit setup for a project `.live/` in `cwd`: creates the dir tree and
 * writes `.live/.gitignore` containing `sessions/` so recorded data stays
 * out of `git status` while `config.json` and the gitignore itself are
 * tracked normally. Returns the absolute `.live/` path. Idempotent.
 */
export async function initLiveDir(cwd: string): Promise<string> {
  const liveDir = join(cwd, LIVE_DIRNAME);
  await ensureLiveDir(liveDir);
  try {
    await writeFile(gitignorePath(liveDir), "sessions/\n", { flag: "wx" });
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
  }
  return liveDir;
}

/**
 * Directory names skipped during `findLiveDirs` because they're never the
 * right place to find a project `.live/` and walking them is expensive.
 */
const SCAN_SKIP_DIRS = new Set([
  "node_modules",
  ".git",
  ".svn",
  ".hg",
]);

/**
 * Recursively scan `root` for `.live/` directories. Does not descend into
 * `.live/` itself or into `SCAN_SKIP_DIRS`. Symlinks are not followed
 * (`Dirent.isDirectory()` returns false for them). Unreadable directories
 * are skipped silently. Returns absolute paths, sorted.
 */
export async function findLiveDirs(root: string): Promise<string[]> {
  const out: string[] = [];
  await walk(root, out);
  out.sort();
  return out;
}

async function walk(dir: string, out: string[]): Promise<void> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }
  const subdirs: string[] = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    if (entry.name === LIVE_DIRNAME) {
      out.push(join(dir, LIVE_DIRNAME));
      continue;
    }
    if (SCAN_SKIP_DIRS.has(entry.name)) continue;
    subdirs.push(entry.name);
  }
  for (const name of subdirs) {
    await walk(join(dir, name), out);
  }
}

async function isDirectory(path: string): Promise<boolean> {
  try {
    const s = await stat(path);
    return s.isDirectory();
  } catch {
    return false;
  }
}
