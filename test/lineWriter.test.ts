import { strict as assert } from "node:assert";
import { open, readFile } from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import { join } from "node:path";
import { describe, test } from "node:test";
import {
  LineWriter,
  type SegmentFiles,
} from "../src/recorder/lineWriter.js";
import { cleanup, mkTmp } from "./_helpers.js";

interface Harness {
  dir: string;
  /** Active segment index, bumped each time `onRotate` fires. */
  active: number;
  read: (n: number) => Promise<{ stream: string; lines: { n: number; t: number }[] }>;
  rotateCount: () => number;
  writer: LineWriter;
  dispose: () => Promise<void>;
}

/**
 * Build a fresh LineWriter writing to numbered files in a tempdir. `onRotate`
 * bumps the segment index and opens a new pair; tests can read back each
 * segment after `dispose()` closes the writer.
 */
async function makeHarness(opts: {
  segmentByteLimit: number;
  clock?: () => number;
}): Promise<Harness> {
  const dir = await mkTmp("line-writer-");
  let active = 0;
  let rotates = 0;

  async function openPair(n: number): Promise<SegmentFiles> {
    return {
      streamFh: await open(join(dir, `stream.${pad(n)}.log`), "a"),
      linesFh: await open(join(dir, `lines.${pad(n)}.log`), "a"),
    };
  }

  const initial = await openPair(active);
  const writer = new LineWriter(initial, {
    segmentByteLimit: opts.segmentByteLimit,
    clock: opts.clock,
    onRotate: async () => {
      rotates += 1;
      active += 1;
      return openPair(active);
    },
  });

  const harness: Harness = {
    dir,
    get active() {
      return active;
    },
    read: async (n) => {
      const stream = await readFile(join(dir, `stream.${pad(n)}.log`), "utf8");
      const linesRaw = await readFile(join(dir, `lines.${pad(n)}.log`), "utf8");
      const lines = linesRaw
        .split("\n")
        .filter((l) => l.length > 0)
        .map((l) => JSON.parse(l));
      return { stream, lines };
    },
    rotateCount: () => rotates,
    writer,
    dispose: async () => {
      await writer.close();
      await cleanup(dir);
    },
  };
  return harness;
}

function pad(n: number): string {
  return n.toString().padStart(4, "0");
}

describe("LineWriter.processChunk — line and chunk boundaries", () => {
  test("a single complete line writes stream + matching record", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    await h.writer.processChunk("hello\n");
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "hello\n");
    assert.equal(lines.length, 1);
    assert.equal(lines[0]!.n, 1);
  });

  test("trailing partial line writes stream bytes but no record", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    await h.writer.processChunk("hello");
    assert.ok(h.writer.hasPartialLine, "should track an in-flight line");
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "hello");
    assert.equal(lines.length, 0);
  });

  test("partial line completed across multiple chunks gets ONE record at first-byte time", async (t) => {
    let clock = 1_000_000;
    const tick = () => clock;
    const h = await makeHarness({ segmentByteLimit: 1024, clock: tick });
    t.after(() => h.dispose());
    await h.writer.processChunk("hel");
    clock = 2_000_000;
    await h.writer.processChunk("lo");
    clock = 3_000_000;
    await h.writer.processChunk("\n");
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "hello\n");
    assert.equal(lines.length, 1);
    // Timestamp must be from the first chunk, not subsequent ones.
    assert.equal(lines[0]!.t, 1_000_000);
    assert.equal(lines[0]!.n, 1);
  });

  test("chunk consisting only of \\n completes the prior in-flight line", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    await h.writer.processChunk("foo");
    await h.writer.processChunk("\n");
    assert.equal(h.writer.hasPartialLine, false);
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "foo\n");
    assert.equal(lines.length, 1);
  });

  test("chunk with multiple complete lines + trailing partial", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    await h.writer.processChunk("a\nb\nc");
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "a\nb\nc");
    assert.equal(lines.length, 2);
    assert.deepEqual(
      lines.map((r) => r.n),
      [1, 2],
    );
    assert.ok(h.writer.hasPartialLine);
  });

  test("write-order invariant: lines records are a prefix of complete stream lines", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    // Interleave complete and partial lines across several chunks; at every
    // observable point the line-record count must equal the complete-stream-
    // line count (the consistency invariant).
    const chunks = ["alpha\nbravo\n", "char", "lie\ndelta\n", "echo"];
    for (const c of chunks) {
      await h.writer.processChunk(c);
    }
    const { stream, lines } = await h.read(0);
    const completeStreamLines = stream.split("\n").length - 1; // last segment is partial "echo"
    assert.equal(completeStreamLines, 4);
    assert.equal(lines.length, 4);
    assert.deepEqual(
      lines.map((r) => r.n),
      [1, 2, 3, 4],
    );
  });

  test("nextLineNumber advances exactly per completed line", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    assert.equal(h.writer.nextLineNumber, 1);
    await h.writer.processChunk("one\n");
    assert.equal(h.writer.nextLineNumber, 2);
    await h.writer.processChunk("two\n");
    assert.equal(h.writer.nextLineNumber, 3);
    await h.writer.processChunk("partial");
    assert.equal(h.writer.nextLineNumber, 3, "partial does not advance");
  });

  test("empty chunk is a no-op", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 1024 });
    t.after(() => h.dispose());
    await h.writer.processChunk("");
    const { stream, lines } = await h.read(0);
    assert.equal(stream, "");
    assert.equal(lines.length, 0);
    assert.equal(h.writer.hasPartialLine, false);
  });
});

describe("LineWriter.processChunk — write faults", () => {
  /**
   * Minimal FileHandle stand-in: tracks writes and can be primed to throw on
   * the Nth call or return a short write. Only `write` and `close` are used
   * by LineWriter; everything else is unused so a cast is safe.
   */
  interface Faulty {
    readonly fh: FileHandle;
    readonly writes: number;
  }
  function makeFaultyFh(opts: {
    throwOn?: number;
    error?: Error;
    shortWriteOn?: number;
  }): Faulty {
    const state = { writes: 0 };
    const fh = {
      write: async (buf: Buffer): Promise<{ bytesWritten: number }> => {
        state.writes += 1;
        if (opts.throwOn !== undefined && state.writes === opts.throwOn) {
          throw opts.error ?? new Error("simulated disk failure");
        }
        if (opts.shortWriteOn !== undefined && state.writes === opts.shortWriteOn) {
          return { bytesWritten: 0 };
        }
        return { bytesWritten: buf.length };
      },
      close: async (): Promise<void> => {},
    } as unknown as FileHandle;
    return {
      fh,
      get writes() {
        return state.writes;
      },
    };
  }

  test("propagates a write error from the stream FileHandle", async () => {
    const stream = makeFaultyFh({ throwOn: 1, error: new Error("ENOSPC: no space left on device") });
    const lines = makeFaultyFh({});
    const writer = new LineWriter(
      { streamFh: stream.fh, linesFh: lines.fh },
      { segmentByteLimit: 1024, onRotate: async () => { throw new Error("unexpected rotate"); } },
    );
    await assert.rejects(writer.processChunk("hello\n"), /ENOSPC/);
    assert.equal(lines.writes, 0, "stream-first invariant: lines fh is never touched when stream throws");
  });

  test("propagates a write error from the lines FileHandle (after stream succeeded)", async () => {
    // This is the SIGKILL-shaped recorder fault: the byte made it to stream
    // but the matching {n,t} record failed. Recorder catches the throw, sets
    // writeError, and stamps deadAt as "inconsistent" on exit.
    const stream = makeFaultyFh({});
    const lines = makeFaultyFh({ throwOn: 1, error: new Error("EIO: i/o error") });
    const writer = new LineWriter(
      { streamFh: stream.fh, linesFh: lines.fh },
      { segmentByteLimit: 1024, onRotate: async () => { throw new Error("unexpected rotate"); } },
    );
    await assert.rejects(writer.processChunk("hello\n"), /EIO/);
    // Stream byte made it; lines write was attempted and threw.
    assert.equal(stream.writes, 1);
    assert.equal(lines.writes, 1);
  });

  test("writeAll throws on a zero-byte short write (no infinite loop)", async () => {
    const stream = makeFaultyFh({ shortWriteOn: 1 });
    const lines = makeFaultyFh({});
    const writer = new LineWriter(
      { streamFh: stream.fh, linesFh: lines.fh },
      { segmentByteLimit: 1024, onRotate: async () => { throw new Error("unexpected rotate"); } },
    );
    await assert.rejects(writer.processChunk("x\n"), /returned 0 bytes/);
  });
});

describe("LineWriter.processChunk — rotation", () => {
  test("rotates after a line crosses segmentByteLimit", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 8 });
    t.after(() => h.dispose());
    // "12345\n" is 6 bytes — under limit. Next "67890\n" pushes past 8.
    await h.writer.processChunk("12345\n");
    assert.equal(h.rotateCount(), 0);
    await h.writer.processChunk("67890\n");
    assert.equal(h.rotateCount(), 1);
    // Segment 0 has both lines (line-never-splits invariant); segment 1 is
    // empty and active.
    const seg0 = await h.read(0);
    assert.equal(seg0.stream, "12345\n67890\n");
    assert.equal(seg0.lines.length, 2);
    const seg1 = await h.read(1);
    assert.equal(seg1.stream, "");
    assert.equal(seg1.lines.length, 0);
  });

  test("a single fat line stays in its starting segment even if it overshoots", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 4 });
    t.after(() => h.dispose());
    // 20-byte line — far past the 4-byte limit, but rotation only happens
    // AFTER the completing newline. The whole line lands in segment 0.
    await h.writer.processChunk("abcdefghijklmnopqrs\n");
    assert.equal(h.rotateCount(), 1, "rotated once after the fat line");
    const seg0 = await h.read(0);
    assert.equal(seg0.stream, "abcdefghijklmnopqrs\n");
    assert.equal(seg0.lines.length, 1);
  });

  test("subsequent lines after rotation go to the new segment", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 6 });
    t.after(() => h.dispose());
    await h.writer.processChunk("aaaaaa\n"); // rotates after this
    await h.writer.processChunk("bbb\n");
    const seg0 = await h.read(0);
    const seg1 = await h.read(1);
    assert.equal(seg0.stream, "aaaaaa\n");
    assert.equal(seg1.stream, "bbb\n");
    assert.equal(seg0.lines.length, 1);
    assert.equal(seg1.lines.length, 1);
    // n increments globally, not per-segment.
    assert.equal(seg0.lines[0]!.n, 1);
    assert.equal(seg1.lines[0]!.n, 2);
  });

  test("trailing partial bytes after rotation land in the new segment", async (t) => {
    const h = await makeHarness({ segmentByteLimit: 4 });
    t.after(() => h.dispose());
    // First chunk: completes a line that triggers rotation, then a partial.
    await h.writer.processChunk("aaaa\nbbb");
    assert.equal(h.rotateCount(), 1);
    const seg0 = await h.read(0);
    const seg1 = await h.read(1);
    assert.equal(seg0.stream, "aaaa\n");
    assert.equal(seg1.stream, "bbb");
    assert.equal(seg0.lines.length, 1);
    assert.equal(seg1.lines.length, 0);
  });
});
