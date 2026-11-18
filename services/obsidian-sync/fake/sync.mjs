import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";

// A test signs the simulated client out by writing this file. A signed-out client exits,
// which is what the supervisor has to notice and report.
const SIGNED_OUT = path.join(process.env.COPPERMIND_DATA_DIR || "/data", "state", "sync", "fake-signed-out");
if (existsSync(SIGNED_OUT)) process.exit(3);

// Stands in for the Obsidian client so the lifecycle can be proved without an
// account. It reports what it is, and the supervisor publishes only what a
// client reports about itself.
process.stdout.write(
  `${JSON.stringify({
    event: "fake sync started",
    sync_mode: "simulated",
    conflict_strategy: "simulated",
  })}\n`,
);

const timer = setInterval(() => {
  if (existsSync(SIGNED_OUT)) process.exit(3);
  process.stdout.write(`${JSON.stringify({ event: "fake sync heartbeat" })}\n`);
}, 1_000);

function stop() {
  clearInterval(timer);
  process.exit(0);
}

process.on("SIGTERM", stop);
process.on("SIGINT", stop);
