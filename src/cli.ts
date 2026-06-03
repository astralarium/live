#!/usr/bin/env node
import { ArgsError, parseArgs } from "./args.js";
import { completionScript } from "./completion/index.js";
import { startMcpServer } from "./mcp/server.js";
import { run as runRecorder } from "./recorder/index.js";
import { initLiveDir } from "./session/discovery.js";

function printHelp(): void {
  process.stdout.write(
    `Usage:
  live <cmd…>               Run <cmd> under a PTY, mirror to your terminal, record to disk.
  live -- <cmd…>            Required if <cmd> starts with '-'; the '-…' namespace is reserved for live's own flags.
  live -n, --name <name>    Tag the session with <name> (agents can filter by it).
  live --init               Create .live/ in cwd with a sessions-only .gitignore.
  live --mcp                Start the MCP server on stdio (for agent clients).
  live --completion <shell> Print completion script for bash | zsh | fish.

Recording lives under the nearest .live/ walking up from cwd;
if none found, falls back to ~/.live/.
`,
  );
}

async function main(): Promise<void> {
  const parsed = parseArgs(process.argv.slice(2));

  switch (parsed.mode) {
    case "help":
      printHelp();
      return;

    case "completion":
      process.stdout.write(completionScript(parsed.shell));
      return;

    case "mcp":
      await startMcpServer();
      return;

    case "init": {
      const liveDir = await initLiveDir(process.cwd());
      process.stdout.write(`Initialized ${liveDir}\n`);
      return;
    }

    case "wrap": {
      if (parsed.command.length === 0) {
        throw new ArgsError("live: no command given");
      }
      const code = await runRecorder({
        cwd: process.cwd(),
        command: parsed.command,
        ...(parsed.name !== undefined ? { name: parsed.name } : {}),
      });
      process.exit(code);
    }
  }
}

main().catch((err) => {
  if (err instanceof ArgsError) {
    process.stderr.write(`${err.message}\n`);
    process.exit(err.exitCode);
  }
  process.stderr.write(`live: ${(err as Error).message}\n`);
  process.exit(1);
});
