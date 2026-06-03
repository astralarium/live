import { strict as assert } from "node:assert";
import { describe, test } from "node:test";
import { cursor, makeCursorState } from "../src/mcp/tools.js";
import { cleanup, makeSession, mkTmp } from "./_helpers.js";

const ID = "01CURSORTESTULIDAAAAAAAAAA";

describe("cursor", () => {
  test("first call places state at lastLine and returns empty", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: { 0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }, { n: 3, t: 3 }] } },
    });
    const state = makeCursorState();
    const r = await cursor(state, { path: sessDir, session_id: ID });
    assert.deepEqual(r.segments, []);
    assert.equal(r.skip_lines, 0);
    assert.equal(r.last_line, 3);
    assert.equal(r.gap, false);
  });

  test("second call (no new lines) returns caught-up empty", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: { 0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] } },
    });
    const state = makeCursorState();
    await cursor(state, { path: sessDir, session_id: ID });
    const r = await cursor(state, { path: sessDir, session_id: ID });
    assert.deepEqual(r.segments, []);
    assert.equal(r.last_line, 2);
    assert.equal(r.gap, false);
  });

  test("since_line override returns segments + skip for the desired range", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: {
        0: { lines: Array.from({ length: 50 }, (_, i) => ({ n: i + 1, t: i })) },
      },
    });
    const state = makeCursorState();
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 10,
    });
    assert.deepEqual(r.segments, ["stream.0000.log"]);
    assert.equal(r.skip_lines, 10);
    assert.equal(r.last_line, 50);
    assert.equal(r.gap, false);
  });

  test("since_line below firstLine returns gap with all retained segments", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    // Simulate post-retention state: firstSegment=2, firstLine=100.
    await makeSession(sessDir, {
      id: ID,
      firstSegment: 2,
      lastSegment: 3,
      segments: {
        2: { lines: [{ n: 100, t: 1 }, { n: 101, t: 2 }] },
        3: { lines: [{ n: 102, t: 3 }] },
      },
    });
    const state = makeCursorState();
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 50,
    });
    assert.equal(r.gap, true);
    assert.deepEqual(r.segments, ["stream.0002.log", "stream.0003.log"]);
    assert.equal(r.last_line, 102);
  });

  test("forward-scan picks the right segment across a multi-segment session", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    // Segments 0..2 hold lines 1-10, 11-20, 21-30.
    await makeSession(sessDir, {
      id: ID,
      firstSegment: 0,
      lastSegment: 2,
      segments: {
        0: { lines: Array.from({ length: 10 }, (_, i) => ({ n: i + 1, t: i })) },
        1: { lines: Array.from({ length: 10 }, (_, i) => ({ n: i + 11, t: i })) },
        2: { lines: Array.from({ length: 10 }, (_, i) => ({ n: i + 21, t: i })) },
      },
    });
    const state = makeCursorState();
    // since_line=15 → next line is 16, which is in segment 1.
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 15,
    });
    assert.deepEqual(r.segments, ["stream.0001.log", "stream.0002.log"]);
    // First record in segment 1 is n=11; skip = 15 - 11 + 1 = 5.
    assert.equal(r.skip_lines, 5);
    assert.equal(r.last_line, 30);
  });

  test("since_line on a segment boundary lands in the next segment", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      firstSegment: 0,
      lastSegment: 1,
      segments: {
        0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] },
        1: { lines: [{ n: 3, t: 3 }, { n: 4, t: 4 }] },
      },
    });
    const state = makeCursorState();
    // since_line=2 → want line 3 onward. Line 3 is the first record of seg1,
    // so the result must be just seg1 with no skip — not seg0+seg1 with
    // skip=2, which would functionally work but leaks a fully-skipped
    // leading segment into the result.
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 2,
    });
    assert.deepEqual(r.segments, ["stream.0001.log"]);
    assert.equal(r.skip_lines, 0);
    assert.equal(r.last_line, 4);
  });

  test("since_line === firstLine returns lines after the floor without gap", async (t) => {
    // Mirror of the segment-boundary test for the lower bound: when the
    // caller's cursor sits exactly at the retained floor, we should return
    // segments starting at firstSegment with skip=1 (skip the floor line
    // itself) — not flag a gap (which fires only when since_line < firstLine).
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      firstSegment: 2,
      lastSegment: 3,
      segments: {
        2: { lines: [{ n: 100, t: 1 }, { n: 101, t: 2 }] },
        3: { lines: [{ n: 102, t: 3 }] },
      },
    });
    const state = makeCursorState();
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 100,
    });
    assert.equal(r.gap, false);
    assert.deepEqual(r.segments, ["stream.0002.log", "stream.0003.log"]);
    assert.equal(r.skip_lines, 1);
    assert.equal(r.last_line, 102);
  });

  test("excludes a just-rotated empty current segment", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      firstSegment: 0,
      lastSegment: 1,
      segments: {
        0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] },
        1: { lines: [] }, // just-rotated
      },
    });
    const state = makeCursorState();
    const r = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 0,
    });
    // segments must not include stream.0001.log even though meta.lastSegment=1.
    assert.deepEqual(r.segments, ["stream.0000.log"]);
    assert.equal(r.last_line, 2);
  });

  test("empty session returns degenerate caught-up", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: { 0: { lines: [] } },
    });
    const state = makeCursorState();
    // First call → place cursor at lastLine=0 and return empty.
    const r1 = await cursor(state, { path: sessDir, session_id: ID });
    assert.deepEqual(r1.segments, []);
    assert.equal(r1.last_line, 0);
    // since_line=5 against empty session → caught-up empty.
    const r2 = await cursor(state, {
      path: sessDir,
      session_id: ID,
      since_line: 5,
    });
    assert.deepEqual(r2.segments, []);
    assert.equal(r2.last_line, 0);
  });

  test("missing meta.json throws McpError (InvalidParams)", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    const state = makeCursorState();
    await assert.rejects(
      cursor(state, { path: sessDir, session_id: ID }),
      /session not found/,
    );
  });

  test("session_id mismatch throws McpError", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: { 0: { lines: [{ n: 1, t: 1 }] } },
    });
    const state = makeCursorState();
    await assert.rejects(
      cursor(state, { path: sessDir, session_id: "01OTHERIDWITHRIGHTLENGTHHH" }),
      /session_id mismatch/,
    );
  });

  test("since_line override updates the tracked cursor", async (t) => {
    const sessDir = await mkTmp();
    t.after(() => cleanup(sessDir));
    await makeSession(sessDir, {
      id: ID,
      segments: { 0: { lines: Array.from({ length: 20 }, (_, i) => ({ n: i + 1, t: i })) } },
    });
    const state = makeCursorState();
    // Initial: place at lastLine=20.
    await cursor(state, { path: sessDir, session_id: ID });
    // Override to since_line=5; next no-arg call should resume from 20 (the new tracked position).
    await cursor(state, { path: sessDir, session_id: ID, since_line: 5 });
    const r = await cursor(state, { path: sessDir, session_id: ID });
    assert.deepEqual(r.segments, []);
    assert.equal(r.last_line, 20);
  });
});
