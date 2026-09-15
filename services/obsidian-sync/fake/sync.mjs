import process from "node:process";

const timer = setInterval(() => {
  process.stdout.write(`${JSON.stringify({ event: "fake sync heartbeat" })}\n`);
}, 1_000);

function stop() {
  clearInterval(timer);
  process.exit(0);
}

process.on("SIGTERM", stop);
process.on("SIGINT", stop);
