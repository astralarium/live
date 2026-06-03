import { strict as assert } from "node:assert";
import { describe, test } from "node:test";
import { ArgsError, parseArgs } from "../src/args.js";

describe("parseArgs", () => {
  test("no args → help", () => {
    assert.deepEqual(parseArgs([]), { mode: "help" });
  });

  test("--help and -h → help", () => {
    assert.deepEqual(parseArgs(["--help"]), { mode: "help" });
    assert.deepEqual(parseArgs(["-h"]), { mode: "help" });
  });

  test("--mcp → mcp", () => {
    assert.deepEqual(parseArgs(["--mcp"]), { mode: "mcp" });
  });

  test("--init → init", () => {
    assert.deepEqual(parseArgs(["--init"]), { mode: "init" });
  });

  test("plain command → wrap with full argv", () => {
    assert.deepEqual(parseArgs(["echo", "hello"]), {
      mode: "wrap",
      command: ["echo", "hello"],
    });
  });

  test("-- consumes the separator and wraps the rest", () => {
    assert.deepEqual(parseArgs(["--", "echo", "-h"]), {
      mode: "wrap",
      command: ["echo", "-h"],
    });
  });

  test("-- escapes commands that collide with live's flags", () => {
    assert.deepEqual(parseArgs(["--", "--mcp"]), {
      mode: "wrap",
      command: ["--mcp"],
    });
  });

  test("--completion bash|zsh|fish → completion", () => {
    for (const shell of ["bash", "zsh", "fish"] as const) {
      assert.deepEqual(parseArgs(["--completion", shell]), {
        mode: "completion",
        shell,
      });
    }
  });

  test("--completion without a shell name throws ArgsError", () => {
    assert.throws(() => parseArgs(["--completion"]), ArgsError);
    assert.throws(() => parseArgs(["--completion"]), /missing shell name/);
  });

  test("--completion with unknown shell throws ArgsError naming the shell", () => {
    assert.throws(() => parseArgs(["--completion", "powershell"]), ArgsError);
    assert.throws(
      () => parseArgs(["--completion", "powershell"]),
      /unknown shell 'powershell'/,
    );
  });

  test("unknown flag throws ArgsError naming the flag", () => {
    assert.throws(() => parseArgs(["--bogus"]), ArgsError);
    assert.throws(() => parseArgs(["--bogus"]), /unknown flag '--bogus'/);
  });

  test("a command starting with '-' requires '--'; error hints at the fix", () => {
    assert.throws(() => parseArgs(["-foo", "bar"]), /unknown flag '-foo'/);
    assert.throws(
      () => parseArgs(["-foo", "bar"]),
      /prefix it with '--': live -- -foo/,
    );
    assert.deepEqual(parseArgs(["--", "-foo", "bar"]), {
      mode: "wrap",
      command: ["-foo", "bar"],
    });
  });

  test("a command whose first token does NOT start with '-' is fine without --", () => {
    // Subsequent argv tokens may be flags to the wrapped command.
    assert.deepEqual(parseArgs(["grep", "-E", "pattern"]), {
      mode: "wrap",
      command: ["grep", "-E", "pattern"],
    });
  });

  test("ArgsError carries exitCode 2 by default", () => {
    try {
      parseArgs(["--bogus"]);
      assert.fail("expected throw");
    } catch (err) {
      assert.ok(err instanceof ArgsError);
      assert.equal(err.exitCode, 2);
    }
  });

  test("--name <value> attaches a name to the wrap", () => {
    assert.deepEqual(parseArgs(["--name", "dev", "pnpm", "dev"]), {
      mode: "wrap",
      command: ["pnpm", "dev"],
      name: "dev",
    });
  });

  test("-n <value> is the short form of --name", () => {
    assert.deepEqual(parseArgs(["-n", "dev", "pnpm", "dev"]), {
      mode: "wrap",
      command: ["pnpm", "dev"],
      name: "dev",
    });
  });

  test("--name carries through -- escape", () => {
    assert.deepEqual(parseArgs(["--name", "dev", "--", "--mcp"]), {
      mode: "wrap",
      command: ["--mcp"],
      name: "dev",
    });
  });

  test("--name without a value throws ArgsError", () => {
    assert.throws(() => parseArgs(["--name"]), /missing value/);
    assert.throws(() => parseArgs(["-n"]), /missing value/);
  });

  test("--name omitted → wrap has no name field", () => {
    const r = parseArgs(["echo", "hi"]);
    assert.equal(r.mode, "wrap");
    assert.equal("name" in r, false);
  });

  test("repeated --name: last value wins", () => {
    assert.deepEqual(parseArgs(["-n", "a", "--name", "b", "cmd"]), {
      mode: "wrap",
      command: ["cmd"],
      name: "b",
    });
  });
});
