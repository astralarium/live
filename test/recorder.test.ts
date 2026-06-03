import { strict as assert } from "node:assert";
import { spawn } from "node:child_process";
import {
  mkdir,
  readFile,
  readdir,
  stat,
  writeFile,
} from "node:fs/promises";
import { join, resolve } from "node:path";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";
import type { Meta } from "../src/session/meta.js";
import { cleanup, mkTmp } from "./_helpers.js";

// Spawn the compiled CLI: tsx resolves relative to the spawned cwd, not
// ours. `pretest` builds dist/ before the suite.
const CLI_JS = resolve(
  fileURLToPath(import.meta.url),
  "..",
  "..",
  "dist",
  "cli.js",
);

/**
 * Spawn `live [--name <n>] -- <cmd>` in `cwd`. Stdin piped (no TTY), stdout
 * silenced, stderr captured. Resolves on child exit.
 */
async function runLive(
  cwd: string,
  command: string[],
  opts: { name?: string } = {},
): Promise<{ code: number; stderr: string }> {
  const liveArgs = opts.name !== undefined ? ["--name", opts.name, "--"] : ["--"];
  return new Promise((resolveP, rejectP) => {
    const child = spawn(
      process.execPath,
      [CLI_JS, ...liveArgs, ...command],
      { cwd, stdio: ["ignore", "ignore", "pipe"] },
    );
    let stderr = "";
    child.stderr.on("data", (b) => {
      stderr += b.toString();
    });
    child.on("error", rejectP);
    child.on("exit", (code) => resolveP({ code: code ?? -1, stderr }));
  });
}

async function readOnlySessionMeta(liveDir: string): Promise<Meta> {
  const sessionsRoot = join(liveDir, "sessions");
  const ids = await readdir(sessionsRoot);
  assert.equal(ids.length, 1, `expected one session, found ${ids.length}`);
  const sessDir = join(sessionsRoot, ids[0]!);
  const meta = JSON.parse(await readFile(join(sessDir, "meta.json"), "utf8")) as Meta;
  return meta;
}

async function readOnlySessionDir(liveDir: string): Promise<string> {
  const sessionsRoot = join(liveDir, "sessions");
  const ids = await readdir(sessionsRoot);
  return join(sessionsRoot, ids[0]!);
}

describe("recorder integration", () => {
  test("records a command's output and finalizes meta on graceful exit", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    const { code } = await runLive(project, [
      "bash",
      "-c",
      "echo hello && echo world",
    ]);
    assert.equal(code, 0);
    const meta = await readOnlySessionMeta(join(project, ".live"));
    assert.equal(meta.status, "exited");
    assert.equal(meta.exitCode, 0);
    assert.ok(meta.exitedAt !== null);
    assert.equal(meta.firstSegment, 0);
    assert.equal(meta.lastSegment, 0);
    const sessDir = await readOnlySessionDir(join(project, ".live"));
    const stream = await readFile(join(sessDir, "stream.0000.log"), "utf8");
    // PTY adds carriage returns; check core substrings.
    assert.match(stream, /hello/);
    assert.match(stream, /world/);
    const lines = await readFile(join(sessDir, "lines.0000.log"), "utf8");
    const records = lines.trim().split("\n").map((l) => JSON.parse(l));
    // At least two records — `hello` and `world`. Allow extra (PTY echo, prompt).
    assert.ok(records.length >= 2, `expected ≥2 line records, got ${records.length}`);
    for (const r of records) {
      assert.equal(typeof r.n, "number");
      assert.equal(typeof r.t, "number");
    }
  });

  test("stamps deadAt as consistent (empty) on graceful exit", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    await runLive(project, ["echo", "hello"]);
    const sessDir = await readOnlySessionDir(join(project, ".live"));
    const deadAt = await readFile(join(sessDir, "deadAt"), "utf8");
    assert.equal(deadAt, "", "graceful exit → empty deadAt = consistent");
  });

  test("rotation: small segmentKb produces multiple segments", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    // 1 KB per segment, 32 KB total retained — leaves room for many segments.
    await writeFile(
      join(project, ".live", "config.json"),
      `{"segmentKb": 1, "maxKb": 32}`,
    );
    const { code } = await runLive(project, [
      "bash",
      "-c",
      "for i in $(seq 1 60); do echo \"line $i with padding to drive rotation\"; done",
    ]);
    assert.equal(code, 0);
    const meta = await readOnlySessionMeta(join(project, ".live"));
    assert.ok(meta.lastSegment > 0, `expected lastSegment > 0, got ${meta.lastSegment}`);
    // All in-range stream segments should exist.
    const sessDir = await readOnlySessionDir(join(project, ".live"));
    for (let n = meta.firstSegment; n <= meta.lastSegment; n++) {
      const segName = `stream.${n.toString().padStart(4, "0")}.log`;
      await stat(join(sessDir, segName)); // throws if missing
    }
  });

  test("retention: tight maxKb unlinks oldest segments and bumps firstSegment", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    // segmentKb=1, maxKb=4 → at steady state, ~4 segments retained.
    // Need to write substantially more than 4 KB total to force retention.
    await writeFile(
      join(project, ".live", "config.json"),
      `{"segmentKb": 1, "maxKb": 4}`,
    );
    const { code } = await runLive(project, [
      "bash",
      "-c",
      // ~80 bytes per line × 300 lines = ~24 KB; well past the 4 KB cap.
      "for i in $(seq 1 300); do echo \"line $i with enough padding text to push the total volume past the retention threshold\"; done",
    ]);
    assert.equal(code, 0);
    const meta = await readOnlySessionMeta(join(project, ".live"));
    // Retention should have advanced firstSegment past 0 — the original
    // segment(s) got unlinked.
    assert.ok(
      meta.firstSegment > 0,
      `expected firstSegment > 0 after retention, got firstSegment=${meta.firstSegment}, lastSegment=${meta.lastSegment}`,
    );
    // The dropped segments must not be on disk.
    const sessDir = await readOnlySessionDir(join(project, ".live"));
    for (let n = 0; n < meta.firstSegment; n++) {
      const segName = `stream.${n.toString().padStart(4, "0")}.log`;
      await assert.rejects(stat(join(sessDir, segName)), { code: "ENOENT" });
    }
  });

  test("--name lands in meta.json; omitted leaves it unset", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    await runLive(project, ["echo", "hi"], { name: "dev" });
    const named = await readOnlySessionMeta(join(project, ".live"));
    assert.equal(named.name, "dev");

    const project2 = await mkTmp();
    t.after(() => cleanup(project2));
    await mkdir(join(project2, ".live"));
    await runLive(project2, ["echo", "hi"]);
    const unnamed = await readOnlySessionMeta(join(project2, ".live"));
    assert.equal(unnamed.name, undefined);
  });

  test("running live does NOT auto-create a gitignore (init is explicit)", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await mkdir(join(project, ".live"));
    await runLive(project, ["echo", "hi"]);
    await assert.rejects(stat(join(project, ".live", ".gitignore")), { code: "ENOENT" });
  });

  test("live --init creates .live/.gitignore that ignores sessions/", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const child = spawn(process.execPath, [CLI_JS, "--init"], {
      cwd: project,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    child.stdout.on("data", (b) => {
      stdout += b.toString();
    });
    const code: number = await new Promise((res) => child.on("exit", (c) => res(c ?? -1)));
    assert.equal(code, 0);
    assert.match(stdout, /^Initialized /);
    const ignore = await readFile(join(project, ".live", ".gitignore"), "utf8");
    assert.equal(ignore, "sessions/\n");
  });
});
