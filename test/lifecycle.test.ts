import { strict as assert } from "node:assert";
import { open, readFile, stat, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, test } from "node:test";
import {
  ensureDeadAt,
  isAlive,
  readDeadAtTime,
  readDeadAtVerdict,
  sweep,
} from "../src/session/lifecycle.js";
import { acquireExclusive } from "../src/util/lock.js";
import { cleanup, makeSession, mkTmp } from "./_helpers.js";

describe("isAlive", () => {
  test("returns false when process.lock does not exist", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await isAlive(dir), false);
  });

  test("returns false when nobody holds the flock", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const fh = await open(join(dir, "process.lock"), "w");
    await fh.close();
    assert.equal(await isAlive(dir), false);
  });

  test("returns true when the flock is held by another fd", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const holder = await open(join(dir, "process.lock"), "w");
    await acquireExclusive(holder.fd);
    try {
      assert.equal(await isAlive(dir), true);
    } finally {
      await holder.close();
    }
    // After release, dead.
    assert.equal(await isAlive(dir), false);
  });
});

describe("ensureDeadAt", () => {
  test("stamps empty deadAt for explicit consistent verdict", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await ensureDeadAt(dir, "consistent");
    const content = await readFile(join(dir, "deadAt"), "utf8");
    assert.equal(content, "");
    assert.equal(await readDeadAtVerdict(dir), "consistent");
  });

  test("stamps inconsistent\\n for explicit inconsistent verdict", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await ensureDeadAt(dir, "inconsistent");
    const content = await readFile(join(dir, "deadAt"), "utf8");
    assert.equal(content, "inconsistent\n");
    assert.equal(await readDeadAtVerdict(dir), "inconsistent");
  });

  test("O_EXCL: second call is a no-op (first verdict wins)", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await ensureDeadAt(dir, "consistent");
    const firstMtime = (await stat(join(dir, "deadAt"))).mtimeMs;
    // Subsequent call with a different verdict must not overwrite.
    await ensureDeadAt(dir, "inconsistent");
    const content = await readFile(join(dir, "deadAt"), "utf8");
    assert.equal(content, "");
    const secondMtime = (await stat(join(dir, "deadAt"))).mtimeMs;
    assert.equal(firstMtime, secondMtime);
  });

  test("O_EXCL: concurrent ensureDeadAt calls produce one verdict (no overwrite, no throw)", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    // Fire many concurrent calls with opposite verdicts. Exactly one should
    // win the O_EXCL race; the rest must observe EEXIST and resolve without
    // either overwriting the verdict or surfacing an error.
    const verdicts: ("consistent" | "inconsistent")[] = [
      "consistent",
      "inconsistent",
      "consistent",
      "inconsistent",
      "consistent",
    ];
    await Promise.all(verdicts.map((v) => ensureDeadAt(dir, v)));
    const finalContent = await readFile(join(dir, "deadAt"), "utf8");
    // Whichever verdict won, the content must match one of the two valid
    // shapes and never be partially-written.
    assert.ok(
      finalContent === "" || finalContent === "inconsistent\n",
      `unexpected deadAt content: ${JSON.stringify(finalContent)}`,
    );
  });

  test("derives verdict from segments when verdict omitted (consistent case)", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await makeSession(dir, {
      segments: {
        0: {
          lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }],
          stream: ["line 1", "line 2"],
        },
      },
    });
    await ensureDeadAt(dir);
    assert.equal(await readDeadAtVerdict(dir), "consistent");
  });

  test("derives inconsistent verdict when stream has one extra line", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await makeSession(dir, {
      segments: {
        0: {
          lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }],
          stream: ["line 1", "line 2", "line 3"], // SIGKILL between stream and lines record
        },
      },
    });
    await ensureDeadAt(dir);
    assert.equal(await readDeadAtVerdict(dir), "inconsistent");
  });
});

describe("readDeadAtTime", () => {
  test("returns null when deadAt absent", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await readDeadAtTime(dir), null);
  });

  test("returns mtime when deadAt present", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await ensureDeadAt(dir, "consistent");
    const t1 = await readDeadAtTime(dir);
    assert.ok(t1 !== null && t1 > 0);
  });
});

describe("sweep", () => {
  test("stamps deadAt on dead sessions", async (t) => {
    const liveDir = await mkTmp();
    t.after(() => cleanup(liveDir));
    const sessionsRoot = join(liveDir, "sessions");
    const sessA = join(sessionsRoot, "01TESTAAAAAAAAAAAAAAAAAAAA");
    const sessB = join(sessionsRoot, "01TESTBBBBBBBBBBBBBBBBBBBB");
    await makeSession(sessA, { id: "01TESTAAAAAAAAAAAAAAAAAAAA" });
    await makeSession(sessB, { id: "01TESTBBBBBBBBBBBBBBBBBBBB" });
    // Both have no process.lock → considered dead.
    await sweep(liveDir, 7);
    assert.equal(await readDeadAtVerdict(sessA), "consistent");
    assert.equal(await readDeadAtVerdict(sessB), "consistent");
  });

  test("does not stamp live sessions", async (t) => {
    const liveDir = await mkTmp();
    t.after(() => cleanup(liveDir));
    const sessDir = join(liveDir, "sessions", "01TESTLIVEAAAAAAAAAAAAAAAA");
    await makeSession(sessDir, { id: "01TESTLIVEAAAAAAAAAAAAAAAA" });
    const holder = await open(join(sessDir, "process.lock"), "w");
    await acquireExclusive(holder.fd);
    try {
      await sweep(liveDir, 7);
      assert.equal(await readDeadAtTime(sessDir), null);
    } finally {
      await holder.close();
    }
  });

  test("deletes session dir whose deadAt is older than ttlDays", async (t) => {
    const liveDir = await mkTmp();
    t.after(() => cleanup(liveDir));
    const sessDir = join(liveDir, "sessions", "01TESTSTALEAAAAAAAAAAAAAAA");
    await makeSession(sessDir, { id: "01TESTSTALEAAAAAAAAAAAAAAA" });
    // Pre-stamp deadAt and backdate its mtime by 10 days.
    await writeFile(join(sessDir, "deadAt"), "");
    const tenDaysAgo = (Date.now() - 10 * 86_400_000) / 1000;
    await utimes(join(sessDir, "deadAt"), tenDaysAgo, tenDaysAgo);
    await sweep(liveDir, 7);
    // Session dir should be gone.
    await assert.rejects(stat(sessDir), { code: "ENOENT" });
  });

  test("keeps a fresh deadAt within ttlDays", async (t) => {
    const liveDir = await mkTmp();
    t.after(() => cleanup(liveDir));
    const sessDir = join(liveDir, "sessions", "01TESTFRESHAAAAAAAAAAAAAAA");
    await makeSession(sessDir, { id: "01TESTFRESHAAAAAAAAAAAAAAA" });
    await sweep(liveDir, 7);
    const s = await stat(sessDir);
    assert.ok(s.isDirectory(), "session dir survives a sweep with fresh deadAt");
  });

  test("is a no-op when sessions/ does not exist", async (t) => {
    const liveDir = await mkTmp();
    t.after(() => cleanup(liveDir));
    // No sessions subdir. sweep should not throw.
    await sweep(liveDir, 7);
  });
});
