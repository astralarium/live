import { rename, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";

/**
 * Write `contents` to a sibling temp file, then rename over `path`. POSIX
 * `rename(2)` is atomic, so readers see the old or new file in full. The
 * temp is unlinked on rename failure so orphans don't accumulate.
 */
export async function atomicWriteFile(
  path: string,
  contents: string | Uint8Array,
): Promise<void> {
  const tmp = join(dirname(path), `.${basename(path)}.${process.pid}.tmp`);
  await writeFile(tmp, contents);
  try {
    await rename(tmp, path);
  } catch (err) {
    await unlink(tmp).catch(() => {});
    throw err;
  }
}
