import { strict as assert } from "node:assert";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, test } from "node:test";
import {
  countCompleteRecords,
  readFirstRecord,
  readLastCompleteRecord,
  sizeOf,
} from "../src/session/segments.js";
import { cleanup, mkTmp } from "./_helpers.js";

describe("readFirstRecord", () => {
  test("returns the first JSONL record", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(
      path,
      `{"n":1,"t":1700000000000}\n{"n":2,"t":1700000000001}\n`,
    );
    const r = await readFirstRecord(path);
    assert.deepEqual(r, { n: 1, t: 1700000000000 });
  });

  test("returns null on empty file", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(path, "");
    assert.equal(await readFirstRecord(path), null);
  });

  test("returns null when first record is still incomplete (no newline)", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(path, `{"n":1,"t":17000`);
    assert.equal(await readFirstRecord(path), null);
  });

  test("returns null on ENOENT", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await readFirstRecord(join(dir, "nope.log")), null);
  });
});

describe("readLastCompleteRecord", () => {
  test("returns the last complete record", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(
      path,
      `{"n":1,"t":1}\n{"n":2,"t":2}\n{"n":3,"t":3}\n`,
    );
    const r = await readLastCompleteRecord(path);
    assert.deepEqual(r, { n: 3, t: 3 });
  });

  test("tolerates a truncated trailing record", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    // Last record is missing its closing brace + newline.
    await writeFile(path, `{"n":1,"t":1}\n{"n":2,"t":2}\n{"n":3,"t":3`);
    const r = await readLastCompleteRecord(path);
    assert.deepEqual(r, { n: 2, t: 2 });
  });

  test("returns null when no record has been completed", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(path, `{"n":1`);
    assert.equal(await readLastCompleteRecord(path), null);
  });

  test("returns null on ENOENT", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await readLastCompleteRecord(join(dir, "nope.log")), null);
  });
});

describe("countCompleteRecords", () => {
  test("counts newline-terminated records, ignoring trailing partial", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "lines.0000.log");
    await writeFile(path, `{"n":1}\n{"n":2}\n{"n":3`);
    assert.equal(await countCompleteRecords(path), 2);
  });

  test("returns 0 on missing file", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await countCompleteRecords(join(dir, "missing.log")), 0);
  });
});

describe("sizeOf", () => {
  test("returns 0 on missing file", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    assert.equal(await sizeOf(join(dir, "missing")), 0);
  });

  test("returns byte size", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const path = join(dir, "f");
    await writeFile(path, "abcde");
    assert.equal(await sizeOf(path), 5);
  });
});
