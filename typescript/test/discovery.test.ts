import { strict as assert } from "node:assert";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, test } from "node:test";
import {
  findAnchor,
  findLiveDirs,
  initLiveDir,
} from "../src/session/discovery.js";
import { cleanup, mkTmp } from "./_helpers.js";

describe("findAnchor", () => {
  test("returns the .live/ in cwd when present", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await mkdir(join(dir, ".live"));
    const anchor = await findAnchor(dir);
    assert.equal(anchor, join(dir, ".live"));
  });

  test("walks up to find .live/ in an ancestor", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    await mkdir(join(root, ".live"));
    const deep = join(root, "a", "b", "c");
    await mkdir(deep, { recursive: true });
    const anchor = await findAnchor(deep);
    assert.equal(anchor, join(root, ".live"));
  });

  test("prefers the nearest .live/ when multiple ancestors have one", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    await mkdir(join(root, ".live"));
    const mid = join(root, "a");
    await mkdir(join(mid, ".live"), { recursive: true });
    const deep = join(mid, "b", "c");
    await mkdir(deep, { recursive: true });
    const anchor = await findAnchor(deep);
    assert.equal(anchor, join(mid, ".live"));
  });
});

describe("findLiveDirs", () => {
  test("returns empty when root has no .live/", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    assert.deepEqual(await findLiveDirs(root), []);
  });

  test("finds a .live/ in root and in nested subdirs", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    await mkdir(join(root, ".live"));
    await mkdir(join(root, "apps", "web", ".live"), { recursive: true });
    const found = await findLiveDirs(root);
    assert.deepEqual(found.sort(), [
      join(root, ".live"),
      join(root, "apps", "web", ".live"),
    ].sort());
  });

  test("does not descend into a found .live/", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    // A nested `.live/` placed inside another `.live/` should not be found —
    // the walker stops at the outer one.
    await mkdir(join(root, ".live", "sessions", "01XXX", ".live"), { recursive: true });
    const found = await findLiveDirs(root);
    assert.deepEqual(found, [join(root, ".live")]);
  });

  test("skips node_modules and .git", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    await mkdir(join(root, "node_modules", "pkg", ".live"), { recursive: true });
    await mkdir(join(root, ".git", "objects", ".live"), { recursive: true });
    await mkdir(join(root, "src", ".live"), { recursive: true });
    const found = await findLiveDirs(root);
    assert.deepEqual(found, [join(root, "src", ".live")]);
  });

  test("silently skips unreadable directories", async (t) => {
    const { chmod } = await import("node:fs/promises");
    const root = await mkTmp();
    const locked = join(root, "locked");
    // Restore perms before removing, otherwise rm -rf can't enter locked/.
    t.after(async () => {
      await chmod(locked, 0o755).catch(() => {});
      await cleanup(root);
    });
    await mkdir(join(root, ".live"));
    await mkdir(locked);
    await chmod(locked, 0o000);
    const found = await findLiveDirs(root);
    assert.deepEqual(found, [join(root, ".live")]);
  });

  test("results are sorted", async (t) => {
    const root = await mkTmp();
    t.after(() => cleanup(root));
    await mkdir(join(root, "z", ".live"), { recursive: true });
    await mkdir(join(root, "a", ".live"), { recursive: true });
    await mkdir(join(root, "m", ".live"), { recursive: true });
    const found = await findLiveDirs(root);
    assert.deepEqual(found, [
      join(root, "a", ".live"),
      join(root, "m", ".live"),
      join(root, "z", ".live"),
    ]);
  });
});

describe("initLiveDir", () => {
  test("creates .live/sessions/ and .live/.gitignore that ignores sessions/", async (t) => {
    const cwd = await mkTmp();
    t.after(() => cleanup(cwd));
    const liveDir = await initLiveDir(cwd);
    assert.equal(liveDir, join(cwd, ".live"));
    assert.ok((await stat(join(liveDir, "sessions"))).isDirectory());
    const ignore = await readFile(join(liveDir, ".gitignore"), "utf8");
    assert.equal(ignore, "sessions/\n");
  });

  test("idempotent: re-running leaves an existing gitignore alone", async (t) => {
    const cwd = await mkTmp();
    t.after(() => cleanup(cwd));
    await initLiveDir(cwd);
    const ignorePath = join(cwd, ".live", ".gitignore");
    await writeFile(ignorePath, "# customized\nsessions/\n");
    await initLiveDir(cwd);
    assert.equal(await readFile(ignorePath, "utf8"), "# customized\nsessions/\n");
  });
});
