import { strict as assert } from "node:assert";
import { readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { after, describe, test } from "node:test";
import { atomicWriteFile } from "../src/util/atomic.js";
import { cleanup, mkTmp } from "./_helpers.js";

describe("atomicWriteFile", () => {
  test("writes the target file with the given contents", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const target = join(dir, "meta.json");
    await atomicWriteFile(target, '{"hello":"world"}');
    const back = await readFile(target, "utf8");
    assert.equal(back, '{"hello":"world"}');
  });

  test("does not leave a temp file behind on success", async (t) => {
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const target = join(dir, "meta.json");
    await atomicWriteFile(target, "ok");
    // Temp form is `.meta.json.<pid>.tmp`. Anything matching `.meta.json.*.tmp`
    // would be a leak.
    await assert.rejects(stat(join(dir, `.meta.json.${process.pid}.tmp`)), {
      code: "ENOENT",
    });
  });

  test("cleans up temp when rename fails", async (t) => {
    // Rename will fail if the target directory doesn't exist; we point the
    // target into a non-existent subdirectory but write the temp into the
    // existing dir by colocating. The implementation puts the temp next to
    // the target (same dirname), so a missing dirname triggers writeFile to
    // fail before rename — not the case we want.
    //
    // Instead, point at a directory whose target component is itself a
    // directory: rename(file, dir) fails with EISDIR on POSIX.
    const dir = await mkTmp();
    t.after(() => cleanup(dir));
    const target = join(dir, "victim");
    // Make `target` an existing directory so rename(file, dir) → EISDIR.
    const { mkdir } = await import("node:fs/promises");
    await mkdir(target);

    await assert.rejects(atomicWriteFile(target, "data"));

    // Temp file must not linger after a failed rename.
    await assert.rejects(stat(join(dir, `.victim.${process.pid}.tmp`)), {
      code: "ENOENT",
    });
  });
});
