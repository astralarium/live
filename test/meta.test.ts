import { strict as assert } from "node:assert";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, test } from "node:test";
import { deriveLines, readMeta, tryReadMeta } from "../src/session/meta.js";
import { cleanup, makeSession, mkTmp } from "./_helpers.js";

describe("readMeta shape validation", () => {
  test("accepts a well-formed meta.json", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await makeSession(dir);
    const m = await readMeta(dir);
    assert.equal(m.firstSegment, 0);
    assert.equal(m.lastSegment, 0);
  });

  test("throws a clear error on malformed meta.json", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await writeFile(
      join(dir, "meta.json"),
      `{"id":"x","missing":"required-fields"}`,
    );
    await assert.rejects(readMeta(dir), /malformed meta\.json/);
  });

  test("throws on wrong type (lastSegment as string)", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const bad = {
      id: "x",
      command: ["echo"],
      cwd: "/tmp",
      startedAt: 1,
      exitedAt: null,
      status: "running",
      exitCode: null,
      firstSegment: 0,
      lastSegment: "1", // string, should be number
    };
    await writeFile(join(dir, "meta.json"), JSON.stringify(bad));
    await assert.rejects(readMeta(dir), /malformed meta\.json/);
  });

  test("tryReadMeta returns null on ENOENT but throws on malformed", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await tryReadMeta(dir), null);
    await writeFile(join(dir, "meta.json"), `{"id":"x"}`);
    await assert.rejects(tryReadMeta(dir), /malformed meta\.json/);
  });

  test("optional name: present is exposed, missing parses as undefined", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    await makeSession(dir, { name: "dev" });
    assert.equal((await readMeta(dir)).name, "dev");

    const dir2 = await mkTmp();
    t.after(() => cleanup(dir2));
    await makeSession(dir2);
    assert.equal((await readMeta(dir2)).name, undefined);
  });

  test("name with wrong type is rejected as malformed", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const bad = {
      id: "x",
      command: ["echo"],
      cwd: "/tmp",
      name: 42,
      startedAt: 1,
      exitedAt: null,
      status: "running",
      exitCode: null,
      firstSegment: 0,
      lastSegment: 0,
    };
    await writeFile(join(dir, "meta.json"), JSON.stringify(bad));
    await assert.rejects(readMeta(dir), /malformed meta\.json/);
  });
});

describe("deriveLines", () => {
  test("brand-new session (no records yet) → empty sentinel", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const meta = await makeSession(dir, {
      segments: { 0: { lines: [] } },
    });
    const d = await deriveLines(dir, meta);
    assert.deepEqual(d, { firstLine: 1, lastLine: 0, count: 0, lastLineSegment: 0 });
  });

  test("populated single-segment session", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const meta = await makeSession(dir, {
      segments: { 0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }, { n: 3, t: 3 }] } },
    });
    const d = await deriveLines(dir, meta);
    assert.deepEqual(d, { firstLine: 1, lastLine: 3, count: 3, lastLineSegment: 0 });
  });

  test("just-rotated current segment → walks back to prior non-empty segment", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const meta = await makeSession(dir, {
      firstSegment: 0,
      lastSegment: 2,
      segments: {
        0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] },
        1: { lines: [{ n: 3, t: 3 }, { n: 4, t: 4 }] },
        2: { lines: [] }, // just-rotated, empty
      },
    });
    const d = await deriveLines(dir, meta);
    assert.equal(d.lastLine, 4);
    assert.equal(d.lastLineSegment, 1, "lastLineSegment walks back to segment 1");
  });

  test("after retention bump (firstSegment > 0) reports new floor", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const meta = await makeSession(dir, {
      firstSegment: 5,
      lastSegment: 6,
      segments: {
        5: { lines: [{ n: 100, t: 1 }, { n: 101, t: 2 }] },
        6: { lines: [{ n: 102, t: 3 }] },
      },
    });
    const d = await deriveLines(dir, meta);
    assert.deepEqual(d, {
      firstLine: 100,
      lastLine: 102,
      count: 3,
      lastLineSegment: 6,
    });
  });

  test("tolerates a phantom lastSegment from a mid-rotation crash", async (t) => {
    // Meta points at lastSegment=2 but no segment 2 files exist on disk —
    // the state right after writeMeta-before-open in rotate(). Readers must
    // walk back rather than crash.
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const meta = await makeSession(dir, {
      firstSegment: 0,
      lastSegment: 2,
      segments: {
        0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] },
        1: { lines: [{ n: 3, t: 3 }] },
        // segment 2: deliberately absent
      },
    });
    const d = await deriveLines(dir, meta);
    assert.equal(d.lastLine, 3);
    assert.equal(d.lastLineSegment, 1);
  });
});
