import { strict as assert } from "node:assert";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, test } from "node:test";
import {
  DEFAULT_CONFIG,
  loadConfig,
  loadConfigFor,
} from "../src/util/config.js";
import { CONFIG_PATH, HOME_LIVE_DIR, configPath } from "../src/util/paths.js";
import { cleanup, mkTmp } from "./_helpers.js";

/**
 * These tests assume `$HOME` was set to a fresh tempdir before node started
 * (see the `test` script in package.json). Each test cleans the home config
 * between runs so they don't bleed into each other.
 */
afterEach(async () => {
  await rm(CONFIG_PATH, { force: true });
});

describe("loadConfig (home)", () => {
  test("auto-creates ~/.live/config.json with defaults when missing", async () => {
    // Sanity: $HOME really is a tempdir, not the real home.
    assert.notEqual(homedir(), process.env["LIVE_REAL_HOME"] ?? null);
    assert.ok(HOME_LIVE_DIR.startsWith(homedir()));

    await rm(CONFIG_PATH, { force: true });
    const config = await loadConfig();
    assert.deepEqual(config, DEFAULT_CONFIG);
    const written = JSON.parse(await readFile(CONFIG_PATH, "utf8"));
    assert.deepEqual(written, DEFAULT_CONFIG);
  });

  test("merges partial home config with defaults", async () => {
    await mkdir(HOME_LIVE_DIR, { recursive: true });
    await writeFile(CONFIG_PATH, `{"maxKb": 1024}`);
    const config = await loadConfig();
    assert.equal(config.maxKb, 1024);
    assert.equal(config.ttlDays, DEFAULT_CONFIG.ttlDays);
    assert.equal(config.segmentKb, DEFAULT_CONFIG.segmentKb);
  });

  test("throws on malformed home JSON", async () => {
    await mkdir(HOME_LIVE_DIR, { recursive: true });
    await writeFile(CONFIG_PATH, "not-json");
    await assert.rejects(loadConfig(), /JSON/);
  });
});

describe("loadConfigFor (per-project layering)", () => {
  test("returns home config when liveDir has no config.json", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const liveDir = join(project, ".live");
    await mkdir(liveDir);
    const config = await loadConfigFor(liveDir);
    assert.deepEqual(config, DEFAULT_CONFIG);
  });

  test("overlays per-project fields on home", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const liveDir = join(project, ".live");
    await mkdir(liveDir);
    await mkdir(HOME_LIVE_DIR, { recursive: true });
    await writeFile(CONFIG_PATH, `{"maxKb": 1024, "ttlDays": 30}`);
    await writeFile(configPath(liveDir), `{"maxKb": 8192}`);
    const config = await loadConfigFor(liveDir);
    assert.equal(config.maxKb, 8192, "per-project wins");
    assert.equal(config.ttlDays, 30, "home survives where project is silent");
    assert.equal(config.segmentKb, DEFAULT_CONFIG.segmentKb, "default for fields missing in both");
  });

  test("tolerates malformed per-project JSON (warns, falls back to home)", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const liveDir = join(project, ".live");
    await mkdir(liveDir);
    await writeFile(configPath(liveDir), "totally not json");

    // Silence the warning we expect.
    const origWarn = console.warn;
    let warned = false;
    console.warn = () => {
      warned = true;
    };
    try {
      const config = await loadConfigFor(liveDir);
      assert.deepEqual(config, DEFAULT_CONFIG);
      assert.ok(warned, "expected a stderr warning");
    } finally {
      console.warn = origWarn;
    }
  });

  test("short-circuits when liveDir is HOME_LIVE_DIR", async () => {
    // No per-project file exists at HOME_LIVE_DIR — short-circuit means we
    // don't even try to read one, so the result equals loadConfig().
    await mkdir(HOME_LIVE_DIR, { recursive: true });
    await writeFile(CONFIG_PATH, `{"maxKb": 256}`);
    const config = await loadConfigFor(HOME_LIVE_DIR);
    assert.equal(config.maxKb, 256);
  });

  test("short-circuit holds even when HOME_LIVE_DIR is reached via a symlink", async (t) => {
    // resolveIncludes canonicalizes via realpath; if HOME sits under a
    // symlink prefix (macOS-style /var/...), the canonical path differs
    // from the lexical HOME_LIVE_DIR. The short-circuit must still fire
    // so we don't redundantly open the home config file as if it were a
    // project file.
    const symlinkParent = await mkTmp();
    t.after(() => cleanup(symlinkParent));
    // Create a symlink in symlinkParent that points to HOME_LIVE_DIR.
    const { symlink } = await import("node:fs/promises");
    await mkdir(HOME_LIVE_DIR, { recursive: true });
    await writeFile(CONFIG_PATH, `{"maxKb": 512}`);
    const aliased = join(symlinkParent, "aliased-home");
    await symlink(HOME_LIVE_DIR, aliased);
    const config = await loadConfigFor(aliased);
    // Should match home; no per-project layer applied (none exists on disk).
    assert.equal(config.maxKb, 512);
  });
});
