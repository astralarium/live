import { strict as assert } from "node:assert";
import { mkdir, open, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, describe, test } from "node:test";
import { listSessions } from "../src/mcp/tools.js";
import { acquireExclusive } from "../src/util/lock.js";
import { CONFIG_PATH } from "../src/util/paths.js";
import { ensureDeadAt } from "../src/session/lifecycle.js";
import { cleanup, makeSession, mkTmp } from "./_helpers.js";

afterEach(async () => {
  // Reset $HOME config between tests so the bad-config case doesn't bleed.
  await import("node:fs/promises").then(({ rm }) => rm(CONFIG_PATH, { force: true }));
});

interface LiveLayout {
  liveDir: string;
  sessionsDir: string;
}

async function makeLive(project: string): Promise<LiveLayout> {
  const liveDir = join(project, ".live");
  const sessionsDir = join(liveDir, "sessions");
  await mkdir(sessionsDir, { recursive: true });
  return { liveDir, sessionsDir };
}

/** Shorthand for the common single-project liveDirs input. */
function dirs(project: string): string[] {
  return [join(project, ".live")];
}

describe("listSessions", () => {
  test("returns no sessions when sessions/ is empty", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await makeLive(project);
    const r = await listSessions(dirs(project), {});
    assert.deepEqual(r, []);
  });

  test("default (include_exited:false) hides dead sessions", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTAAAAAAAAAAAAAAAAAAAA";
    await makeSession(join(sessionsDir, id), { id });
    const r = await listSessions(dirs(project), {});
    assert.deepEqual(r, [], "no process.lock → dead → filtered out");
  });

  test("include_exited:true surfaces dead sessions", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTBBBBBBBBBBBBBBBBBBBB";
    const sessDir = join(sessionsDir, id);
    await makeSession(sessDir, {
      id,
      segments: { 0: { lines: [{ n: 1, t: 1 }, { n: 2, t: 2 }] } },
    });
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.equal(r.length, 1);
    assert.equal(r[0]!.id, id);
    assert.equal(r[0]!.status, "exited");
    assert.equal(r[0]!.consistent, true, "no inconsistent verdict → defaults true");
    assert.equal(r[0]!.firstLine, 1);
    assert.equal(r[0]!.lastLine, 2);
    assert.equal(r[0]!.count, 2);
  });

  test("running session (held flock) reports status=running without include_exited", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTCCCCCCCCCCCCCCCCCCCC";
    const sessDir = join(sessionsDir, id);
    await makeSession(sessDir, {
      id,
      segments: { 0: { lines: [{ n: 1, t: 1 }] } },
    });
    const holder = await open(join(sessDir, "process.lock"), "w");
    await acquireExclusive(holder.fd);
    try {
      const r = await listSessions(dirs(project), {});
      assert.equal(r.length, 1);
      assert.equal(r[0]!.id, id);
      assert.equal(r[0]!.status, "running");
    } finally {
      await holder.close();
    }
  });

  test("consistent=false when deadAt stamps an inconsistent verdict", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTDDDDDDDDDDDDDDDDDDDD";
    const sessDir = join(sessionsDir, id);
    await makeSession(sessDir, { id });
    await ensureDeadAt(sessDir, "inconsistent");
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.equal(r.length, 1);
    assert.equal(r[0]!.consistent, false);
  });

  test("exitedAt: meta value wins over deadAt mtime", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTEEEEEEEEEEEEEEEEEEEE";
    const sessDir = join(sessionsDir, id);
    // Graceful exit: meta.exitedAt is populated.
    await makeSession(sessDir, {
      id,
      exitedAt: 1_700_000_500_000,
      exitCode: 0,
      status: "exited",
    });
    await ensureDeadAt(sessDir, "consistent");
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.equal(r[0]!.exitedAt, 1_700_000_500_000);
    assert.equal(r[0]!.exitCode, 0);
  });

  test("exitedAt: falls back to deadAt mtime when meta.exitedAt is null", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTFFFFFFFFFFFFFFFFFFFF";
    const sessDir = join(sessionsDir, id);
    // Crash: meta.exitedAt null; exitedAt comes from deadAt mtime.
    await makeSession(sessDir, { id });
    const before = Date.now();
    await ensureDeadAt(sessDir, "consistent");
    const after = Date.now();
    const r = await listSessions(dirs(project), { include_exited: true });
    const exitedAt = r[0]!.exitedAt;
    assert.ok(
      typeof exitedAt === "number" && exitedAt >= before - 1 && exitedAt <= after + 1,
      `exitedAt ${exitedAt} should be near stamp time [${before},${after}]`,
    );
  });

  test("omits exitCode when meta.exitCode is null", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTGGGGGGGGGGGGGGGGGGGG";
    await makeSession(join(sessionsDir, id), { id });
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.equal("exitCode" in r[0]!, false);
  });

  test("entries sort by id descending (newest first)", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const idA = "01AAAAAAAAAAAAAAAAAAAAAAAA";
    const idB = "01BBBBBBBBBBBBBBBBBBBBBBBB";
    const idC = "01CCCCCCCCCCCCCCCCCCCCCCCC";
    // Create out of order to verify the sort step actually runs.
    await makeSession(join(sessionsDir, idA), { id: idA });
    await makeSession(join(sessionsDir, idC), { id: idC });
    await makeSession(join(sessionsDir, idB), { id: idB });
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.deepEqual(r.map((e) => e.id), [idC, idB, idA]);
  });

  test("skips sessions with missing meta.json", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    // A half-created session: dir exists, no meta yet.
    await mkdir(join(sessionsDir, "01HALFLIFEAAAAAAAAAAAAAAAA"), { recursive: true });
    const id = "01LISTHHHHHHHHHHHHHHHHHHHH";
    await makeSession(join(sessionsDir, id), { id });
    const r = await listSessions(dirs(project), { include_exited: true });
    assert.equal(r.length, 1);
    assert.equal(r[0]!.id, id);
  });

  test("aggregates sessions across multiple liveDirs", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { liveDir: anchorLive, sessionsDir: anchorSessions } = await makeLive(project);
    const sub = join(project, "sub");
    await mkdir(sub, { recursive: true });
    const { liveDir: subLive, sessionsDir: subSessions } = await makeLive(sub);
    const idAnchor = "01LISTANCHORAAAAAAAAAAAAAA";
    const idSub = "01LISTSUBAAAAAAAAAAAAAAAAA";
    await makeSession(join(anchorSessions, idAnchor), { id: idAnchor });
    await makeSession(join(subSessions, idSub), { id: idSub });
    const r = await listSessions([anchorLive, subLive], { include_exited: true });
    const ids = r.map((e) => e.id).sort();
    assert.deepEqual(ids, [idAnchor, idSub].sort());
  });

  test("entry surfaces meta.name when set, omits it otherwise", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const idNamed = "01LISTNAMEDAAAAAAAAAAAAAAA";
    const idUnnamed = "01LISTNAMEDBBBBBBBBBBBBBBB";
    await makeSession(join(sessionsDir, idNamed), { id: idNamed, name: "dev" });
    await makeSession(join(sessionsDir, idUnnamed), { id: idUnnamed });
    const r = await listSessions(dirs(project), { include_exited: true });
    const named = r.find((e) => e.id === idNamed)!;
    const unnamed = r.find((e) => e.id === idUnnamed)!;
    assert.equal(named.name, "dev");
    assert.equal("name" in unnamed, false);
  });

  test("name filter selects matching sessions; first is most recent", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const idOlder = "01LISTFILTERAAAAAAAAAAAAAA";
    const idNewer = "01LISTFILTERBBBBBBBBBBBBBB";
    const idOther = "01LISTFILTERCCCCCCCCCCCCCC";
    await makeSession(join(sessionsDir, idOlder), { id: idOlder, name: "dev" });
    await makeSession(join(sessionsDir, idNewer), { id: idNewer, name: "dev" });
    await makeSession(join(sessionsDir, idOther), { id: idOther, name: "build" });
    const r = await listSessions(dirs(project), { include_exited: true, name: "dev" });
    assert.deepEqual(r.map((e) => e.id), [idNewer, idOlder]);
    // The agent's "most recent dev session" is the first entry.
    assert.equal(r[0]!.id, idNewer);
  });

  test("name filter returns empty when no session matches", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    const { sessionsDir } = await makeLive(project);
    const id = "01LISTFILTERZZZZZZZZZZZZZZ";
    await makeSession(join(sessionsDir, id), { id, name: "dev" });
    const r = await listSessions(dirs(project), { include_exited: true, name: "missing" });
    assert.deepEqual(r, []);
  });

  test("throws McpError on bad home config", async (t) => {
    const project = await mkTmp();
    t.after(() => cleanup(project));
    await makeLive(project);
    // Bad home config must propagate (per layering contract).
    await mkdir(CONFIG_PATH.replace(/\/[^/]+$/, ""), { recursive: true });
    await writeFile(CONFIG_PATH, "not-json");
    await assert.rejects(listSessions(dirs(project), {}), /failed to load config/);
  });
});
