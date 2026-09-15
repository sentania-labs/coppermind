import process from "node:process";

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
  process.stdout.write(`${JSON.stringify({ event: "fake sync heartbeat" })}\n`);
}, 1_000);

function stop() {
  clearInterval(timer);
  process.exit(0);
}

process.on("SIGTERM", stop);
process.on("SIGINT", stop);
