import { flock } from "fs-ext";

type FlockMode = "sh" | "ex" | "shnb" | "exnb" | "un";

function flockAsync(fd: number, mode: FlockMode): Promise<void> {
  return new Promise((resolve, reject) => {
    flock(fd, mode, (err) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

/**
 * Try to acquire an exclusive flock on `fd` (non-blocking). Returns true if
 * acquired, false if another process holds it. The kernel releases the lock
 * when the holder exits — this is the liveness probe's load-bearing property.
 */
export async function tryAcquireExclusive(fd: number): Promise<boolean> {
  try {
    await flockAsync(fd, "exnb");
    return true;
  } catch (err) {
    const e = err as NodeJS.ErrnoException;
    if (e.code === "EAGAIN" || e.code === "EWOULDBLOCK") return false;
    throw err;
  }
}

/** Block until the exclusive lock on `fd` is acquired. */
export async function acquireExclusive(fd: number): Promise<void> {
  await flockAsync(fd, "ex");
}
