import type { FileHandle } from "node:fs/promises";

export interface SegmentFiles {
  streamFh: FileHandle;
  linesFh: FileHandle;
}

export interface LineWriterOptions {
  segmentByteLimit: number;
  /**
   * Called once the active segment fills. Returns the next pair of handles;
   * `LineWriter` closes the old pair before calling.
   */
  onRotate: () => Promise<SegmentFiles>;
  /** Injectable for deterministic tests. Defaults to `Date.now`. */
  clock?: () => number;
}

/**
 * Stream bytes first, `{n,t}` record second; rotate when the active stream
 * segment crosses `segmentByteLimit`. Invariant: `lines.*.log` records are
 * always a prefix of the complete lines in `stream.*.log`.
 */
export class LineWriter {
  private streamFh: FileHandle;
  private linesFh: FileHandle;
  private currentSegmentBytes = 0;
  private currentT: number | null = null;
  private nextN = 1;
  private readonly clock: () => number;

  constructor(initial: SegmentFiles, private readonly opts: LineWriterOptions) {
    this.streamFh = initial.streamFh;
    this.linesFh = initial.linesFh;
    this.clock = opts.clock ?? Date.now;
  }

  /**
   * Append one chunk. A line's timestamp is the time of its first byte,
   * preserved across chunks. Rotates when a completed line carries the
   * active segment past `segmentByteLimit`.
   */
  async processChunk(data: string): Promise<void> {
    const bytes = Buffer.from(data, "utf8");
    let lineStart = 0;
    for (let i = 0; i < bytes.length; i++) {
      if (this.currentT === null) this.currentT = this.clock();
      if (bytes[i] === 0x0a) {
        const lineBytes = bytes.subarray(lineStart, i + 1);
        await writeAll(this.streamFh, lineBytes);
        this.currentSegmentBytes += lineBytes.length;
        const record = JSON.stringify({ n: this.nextN, t: this.currentT }) + "\n";
        await writeAll(this.linesFh, Buffer.from(record, "utf8"));
        this.nextN += 1;
        this.currentT = null;
        lineStart = i + 1;
        if (this.currentSegmentBytes >= this.opts.segmentByteLimit) {
          await this.rotate();
        }
      }
    }
    if (lineStart < bytes.length) {
      const tailBytes = bytes.subarray(lineStart);
      await writeAll(this.streamFh, tailBytes);
      this.currentSegmentBytes += tailBytes.length;
    }
  }

  /** Close the active segment pair. */
  async close(): Promise<void> {
    await this.streamFh.close();
    await this.linesFh.close();
  }

  /** Next line number that would be emitted. */
  get nextLineNumber(): number {
    return this.nextN;
  }

  /** True iff bytes have been written for a line that hasn't terminated yet. */
  get hasPartialLine(): boolean {
    return this.currentT !== null;
  }

  private async rotate(): Promise<void> {
    await this.streamFh.close();
    await this.linesFh.close();
    const next = await this.opts.onRotate();
    this.streamFh = next.streamFh;
    this.linesFh = next.linesFh;
    this.currentSegmentBytes = 0;
  }
}

/**
 * Loop until `buf` is fully written. Node permits short writes; a torn
 * record would defeat the prefix invariant.
 */
async function writeAll(fh: FileHandle, buf: Buffer): Promise<void> {
  let offset = 0;
  while (offset < buf.length) {
    const { bytesWritten } = await fh.write(buf, offset, buf.length - offset);
    if (bytesWritten === 0) {
      throw new Error("FileHandle.write returned 0 bytes");
    }
    offset += bytesWritten;
  }
}
