import { readFile, realpath, writeFile } from "node:fs/promises";
import { CONFIG_PATH, HOME_LIVE_DIR, configPath } from "./paths.js";
import { ensureLiveDir } from "../session/discovery.js";

export interface Config {
  ttlDays: number;
  maxKb: number;
  segmentKb: number;
}

export const DEFAULT_CONFIG: Config = {
  ttlDays: 7,
  maxKb: 512,
  segmentKb: 64,
};

/** Load `~/.live/config.json`, creating it with defaults if absent. */
export async function loadConfig(): Promise<Config> {
  let raw;
  try {
    raw = await readFile(CONFIG_PATH, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      await ensureLiveDir(HOME_LIVE_DIR);
      await writeFile(
        CONFIG_PATH,
        JSON.stringify(DEFAULT_CONFIG, null, 2) + "\n",
        { flag: "wx" },
      ).catch((err) => {
        if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
      });
      raw = await readFile(CONFIG_PATH, "utf8");
    } else {
      throw err;
    }
  }
  return mergeConfig(DEFAULT_CONFIG, JSON.parse(raw) as Partial<Config>);
}

/**
 * Resolve the effective config for `liveDir`: per-project over home over
 * defaults, merged field-by-field. A malformed per-project file is logged
 * and ignored; a malformed home file throws so callers can choose policy.
 */
export async function loadConfigFor(liveDir: string): Promise<Config> {
  const base = await loadConfig();
  if (await isHomeLiveDir(liveDir)) return base;
  const localPath = configPath(liveDir);
  let raw: string;
  try {
    raw = await readFile(localPath, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return base;
    throw err;
  }
  try {
    return mergeConfig(base, JSON.parse(raw) as Partial<Config>);
  } catch (err) {
    console.warn(`[live] ignoring ${localPath}: ${(err as Error).message}`);
    return base;
  }
}

let homeRealpathCache: string | null = null;

/** Compare via realpath so symlinked paths (e.g. macOS `/var → /private/var`)
 * still match. */
async function isHomeLiveDir(liveDir: string): Promise<boolean> {
  if (liveDir === HOME_LIVE_DIR) return true;
  if (homeRealpathCache === null) {
    try {
      homeRealpathCache = await realpath(HOME_LIVE_DIR);
    } catch {
      homeRealpathCache = HOME_LIVE_DIR;
    }
  }
  try {
    return (await realpath(liveDir)) === homeRealpathCache;
  } catch {
    return false;
  }
}

function mergeConfig(base: Config, over: Partial<Config>): Config {
  const merged: Config = {
    ttlDays: over.ttlDays ?? base.ttlDays,
    maxKb: over.maxKb ?? base.maxKb,
    segmentKb: over.segmentKb ?? base.segmentKb,
  };
  validateConfig(merged);
  return merged;
}

/** `ttlDays >= 0`, `maxKb` and `segmentKb` strictly positive, all finite. */
function validateConfig(cfg: Config): void {
  if (!Number.isFinite(cfg.ttlDays) || cfg.ttlDays < 0) {
    throw new Error(
      `config.ttlDays must be a non-negative finite number, got ${cfg.ttlDays}`,
    );
  }
  if (!Number.isFinite(cfg.maxKb) || cfg.maxKb <= 0) {
    throw new Error(
      `config.maxKb must be a positive finite number, got ${cfg.maxKb}`,
    );
  }
  if (!Number.isFinite(cfg.segmentKb) || cfg.segmentKb <= 0) {
    throw new Error(
      `config.segmentKb must be a positive finite number, got ${cfg.segmentKb}`,
    );
  }
}
