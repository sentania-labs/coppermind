import { readFile } from "node:fs/promises";
import process from "node:process";

const tokenFile =
  process.env.COPPERMIND_INTERNAL_TOKEN_FILE || "/run/coppermind/internal/internal-token";
const command = process.argv[2] || "status";
const routes = {
  status: ["GET", "/status"],
  connect: ["POST", "/connect"],
  pause: ["POST", "/pause"],
  resume: ["POST", "/resume"],
};
if (!(command in routes)) {
  process.stderr.write(
    "usage: node /app/control.mjs status|connect <remote-vault-name>|pause|resume\n",
  );
  process.exit(2);
}

const token = (await readFile(tokenFile, "utf8")).trim();
const [method, route] = routes[command];
const body = command === "connect" ? { vault_name: process.argv[3] } : {};
const port = process.env.COPPERMIND_SYNC_PORT || "8092";
const response = await fetch(`http://127.0.0.1:${port}${route}`, {
  method,
  headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
  body: method === "POST" ? JSON.stringify(body) : undefined,
});
process.stdout.write(await response.text());
process.exit(response.ok ? 0 : 1);
