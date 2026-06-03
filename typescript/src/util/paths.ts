import { homedir } from "node:os";
import { join } from "node:path";

export const LIVE_DIRNAME = ".live";
export const HOME_LIVE_DIR = join(homedir(), LIVE_DIRNAME);

export const SESSIONS_DIRNAME = "sessions";
export const CONFIG_FILENAME = "config.json";
export const GITIGNORE_FILENAME = ".gitignore";
export const META_FILENAME = "meta.json";
export const PROCESS_LOCK_FILENAME = "process.lock";
export const DEAD_AT_FILENAME = "deadAt";

export function configPath(liveDir: string): string {
  return join(liveDir, CONFIG_FILENAME);
}

export const CONFIG_PATH = configPath(HOME_LIVE_DIR);

export function sessionsDir(liveDir: string): string {
  return join(liveDir, SESSIONS_DIRNAME);
}

export function sessionDir(liveDir: string, sessionId: string): string {
  return join(sessionsDir(liveDir), sessionId);
}

export function metaPath(sessDir: string): string {
  return join(sessDir, META_FILENAME);
}

export function processLockPath(sessDir: string): string {
  return join(sessDir, PROCESS_LOCK_FILENAME);
}

export function deadAtPath(sessDir: string): string {
  return join(sessDir, DEAD_AT_FILENAME);
}

export function gitignorePath(dir: string): string {
  return join(dir, GITIGNORE_FILENAME);
}
