import { isShell, type Shell } from "./completion/index.js";

export type ParsedArgs =
  | { mode: "help" }
  | { mode: "mcp" }
  | { mode: "init" }
  | { mode: "completion"; shell: Shell }
  | { mode: "wrap"; command: string[]; name?: string };

/** Argv parse failure. The CLI exits with `exitCode` after printing `message`. */
export class ArgsError extends Error {
  constructor(message: string, readonly exitCode: number = 2) {
    super(message);
    this.name = "ArgsError";
  }
}

/**
 * Parse `argv[2..]` into a tagged `ParsedArgs`. Throws `ArgsError` on bad
 * input (pure: no stderr, no `process.exit`).
 */
export function parseArgs(argv: string[]): ParsedArgs {
  let name: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--") {
      return wrap(argv.slice(i + 1), name);
    }
    if (a === "--mcp") {
      return { mode: "mcp" };
    }
    if (a === "--init") {
      return { mode: "init" };
    }
    if (a === "--completion") {
      const shell = argv[i + 1];
      if (!shell) {
        throw new ArgsError(
          "live --completion: missing shell name (bash|zsh|fish)",
        );
      }
      if (!isShell(shell)) {
        throw new ArgsError(
          `live --completion: unknown shell '${shell}' (expected bash|zsh|fish)`,
        );
      }
      return { mode: "completion", shell };
    }
    if (a === "--help" || a === "-h") {
      return { mode: "help" };
    }
    if (a === "--name" || a === "-n") {
      const v = argv[i + 1];
      if (v === undefined) {
        throw new ArgsError(`live ${a}: missing value`);
      }
      name = v;
      i += 1;
      continue;
    }
    if (a !== undefined && !a.startsWith("-")) {
      return wrap(argv.slice(i), name);
    }
    // Reserve the `-…` namespace for live's own flags.
    throw new ArgsError(
      `live: unknown flag '${a}' (if this is your command, prefix it with '--': live -- ${a} …)`,
    );
  }
  return { mode: "help" };
}

function wrap(command: string[], name: string | undefined): ParsedArgs {
  return name !== undefined
    ? { mode: "wrap", command, name }
    : { mode: "wrap", command };
}
