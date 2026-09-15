import { timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";

const DATA_DIR = process.env.COPPERMIND_DATA_DIR || "/data";
const STATE_DIR = path.join(DATA_DIR, "state", "sync");
const STATUS_FILE = path.join(STATE_DIR, "status.json");
const CONNECTION_FILE = path.join(STATE_DIR, "connection.json");
const TOKEN_FILE =
  process.env.COPPERMIND_INTERNAL_TOKEN_FILE || "/run/coppermind/internal/internal-token";
const FAKE = process.env.COPPERMIND_SYNC_FAKE === "1";
const PORT = Number.parseInt(process.env.COPPERMIND_SYNC_PORT || "8092", 10);
const LOG_LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 };
const LOG_THRESHOLD =
  LOG_LEVELS[(process.env.COPPERMIND_LOG_LEVEL || "INFO").trim().toUpperCase()] ?? LOG_LEVELS.INFO;
// The simulated client is the only supervised child today, so these are its
// restart bounds and nothing else's.
const RESTART_BASE_MS = 250;
const RESTART_CEILING_MS = 2_000;
const RESTART_STABLE_MS = 2_000;

// Nothing here may reach the operator's Obsidian account or their remote vault
// object. First-connect behaviour and where the account credential lives are
// both open captain decisions, so every path that would run the real client
// refuses instead and says why.
const REAL_SYNC_REFUSED =
  "real Obsidian sync is refused pending captain decisions on first-connect behaviour and credential placement";

let child = null;
let childStartedAt = 0;
let restartTimer = null;
let restartAttempts = 0;
let stopping = false;
let connection = null;
let writeSequence = 0;
let writeQueue = Promise.resolve();

const status = {
  schema_version: 1,
  state: "not_connected",
  connected: false,
  configured: false,
  syncing: false,
  paused: false,
  // True whenever the supervised child is the bundled simulator rather than
  // the Obsidian client. Nothing in a simulated run reaches a remote vault.
  simulated: FAKE,
  real_sync_supported: false,
  vault_name: null,
  // Only ever set from what the running client reports about itself.
  sync_mode: null,
  conflict_strategy: null,
  // The supervisor watches the child process and nothing else: a client that
  // is running but has stopped delivering files still reports syncing.
  liveness: "child_process_only",
  last_error: null,
  sync_pid: null,
  control_port: null,
};

function log(level, event, fields = {}) {
  if (LOG_LEVELS[level] < LOG_THRESHOLD) return;
  process.stdout.write(
    `${JSON.stringify({
      timestamp: new Date().toISOString(),
      level: level.toLowerCase(),
      service: "obsidian-sync",
      event,
      ...fields,
    })}\n`,
  );
}

async function writeJsonAtomically(file, value) {
  await mkdir(path.dirname(file), { recursive: true });
  writeSequence += 1;
  const temporary = `${file}.${process.pid}.${writeSequence}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await rename(temporary, file);
}

async function publishStatus(patch = {}) {
  Object.assign(status, patch);
  const snapshot = { ...status };
  writeQueue = writeQueue.catch(() => {}).then(() => writeJsonAtomically(STATUS_FILE, snapshot));
  await writeQueue;
}

async function loadConnection() {
  try {
    const value = JSON.parse(await readFile(CONNECTION_FILE, "utf8"));
    if (typeof value.vault_name !== "string" || value.vault_name.length === 0) {
      throw new Error("connection file has no remote vault name");
    }
    connection = value;
    const paused = FAKE && value.paused === true;
    Object.assign(status, {
      configured: true,
      paused,
      vault_name: value.vault_name,
      state: FAKE ? (paused ? "paused" : "starting") : "refused",
      last_error: FAKE ? null : REAL_SYNC_REFUSED,
    });
  } catch (error) {
    if (error.code !== "ENOENT") {
      Object.assign(status, { state: "error", last_error: String(error.message || error) });
    }
  }
}

async function saveConnection() {
  await writeJsonAtomically(CONNECTION_FILE, connection);
}

function observeClientReport(line) {
  let parsed;
  try {
    parsed = JSON.parse(line);
  } catch {
    return;
  }
  const patch = {};
  if (typeof parsed.sync_mode === "string") patch.sync_mode = parsed.sync_mode;
  if (typeof parsed.conflict_strategy === "string") {
    patch.conflict_strategy = parsed.conflict_strategy;
  }
  if (Object.keys(patch).length > 0) publishStatus(patch).catch(() => {});
}

async function stopChild() {
  if (!child) return;
  const proc = child;
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      proc.kill("SIGKILL");
      resolve();
    }, 2_000);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    proc.kill("SIGTERM");
  });
}

async function startSync() {
  if (!connection || status.paused || stopping || child) return;
  if (!FAKE) {
    await publishStatus({
      state: "refused",
      connected: false,
      syncing: false,
      sync_pid: null,
      last_error: REAL_SYNC_REFUSED,
    });
    log("WARNING", "real sync refused", { vault_name: status.vault_name });
    return;
  }
  await publishStatus({ state: "starting", connected: false, syncing: false, sync_pid: null });
  const proc = spawn(process.execPath, [path.join(import.meta.dirname, "fake", "sync.mjs")], {
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child = proc;
  childStartedAt = Date.now();
  proc.stdout.on("data", (chunk) => {
    const output = chunk.toString().trim();
    log("INFO", "sync output", { output });
    for (const line of output.split("\n")) observeClientReport(line);
  });
  proc.stderr.on("data", (chunk) =>
    log("WARNING", "sync error output", { output: chunk.toString().trim() }),
  );
  proc.once("error", (error) => handleChildExit(proc, null, null, error));
  proc.once("exit", (code, signal) => handleChildExit(proc, code, signal));
  await publishStatus({
    state: "syncing",
    connected: true,
    syncing: true,
    sync_pid: proc.pid,
    last_error: null,
  });
  log("INFO", "sync process started", { pid: proc.pid, simulated: status.simulated });
}

function scheduleRestart() {
  if (!connection || status.paused || stopping || !FAKE) return;
  clearTimeout(restartTimer);
  const delay = Math.min(RESTART_BASE_MS * 2 ** restartAttempts, RESTART_CEILING_MS);
  restartAttempts += 1;
  log("INFO", "sync restart scheduled", { attempt: restartAttempts, delay_ms: delay });
  restartTimer = setTimeout(() => startSync().catch(() => {}), delay);
}

async function handleChildExit(proc, code, signal, error = null) {
  if (child !== proc) return;
  child = null;
  if (Date.now() - childStartedAt >= RESTART_STABLE_MS) restartAttempts = 0;
  const expected = stopping || status.paused;
  const message = error
    ? String(error.message || error)
    : `sync process exited (${code ?? signal ?? "unknown"})`;
  await publishStatus({
    state: expected ? (status.paused ? "paused" : "stopping") : "restarting",
    connected: false,
    syncing: false,
    sync_pid: null,
    last_error: expected ? null : message,
  });
  if (!expected) {
    log("WARNING", "sync process stopped unexpectedly", { code, signal });
    scheduleRestart();
  }
}

async function connect(body) {
  if (!FAKE) return [501, { error: "real_sync_refused", detail: REAL_SYNC_REFUSED }];
  const vaultName = body.vault_name;
  if (typeof vaultName !== "string" || vaultName.length === 0) {
    return [400, { error: "vault_name_required" }];
  }
  if (child && status.syncing) return [409, { error: "already_connected" }];
  connection = { vault_name: vaultName, paused: false };
  restartAttempts = 0;
  await saveConnection();
  await publishStatus({ configured: true, paused: false, vault_name: vaultName });
  await startSync();
  return [200, publicStatus()];
}

async function pause() {
  if (!FAKE) return [501, { error: "real_sync_refused", detail: REAL_SYNC_REFUSED }];
  if (!connection) return [409, { error: "not_configured" }];
  connection.paused = true;
  status.paused = true;
  clearTimeout(restartTimer);
  await saveConnection();
  await stopChild();
  await publishStatus({ state: "paused", connected: false, syncing: false, sync_pid: null });
  return [200, publicStatus()];
}

async function resume() {
  if (!FAKE) return [501, { error: "real_sync_refused", detail: REAL_SYNC_REFUSED }];
  if (!connection) return [409, { error: "not_configured" }];
  connection.paused = false;
  status.paused = false;
  restartAttempts = 0;
  await saveConnection();
  await startSync();
  return [200, publicStatus()];
}

function publicStatus() {
  return { ...status };
}

async function authorized(request) {
  const supplied = request.headers.authorization?.replace(/^Bearer /, "") || "";
  try {
    const expected = (await readFile(TOKEN_FILE, "utf8")).trim();
    const left = Buffer.from(supplied);
    const right = Buffer.from(expected);
    return left.length === right.length && left.length > 0 && timingSafeEqual(left, right);
  } catch {
    return false;
  }
}

async function readBody(request) {
  let raw = "";
  for await (const chunk of request) {
    raw += chunk;
    if (raw.length > 65_536) throw new Error("request body is too large");
  }
  return raw ? JSON.parse(raw) : {};
}

function send(response, code, body) {
  response.writeHead(code, { "content-type": "application/json" });
  response.end(`${JSON.stringify(body)}\n`);
}

const server = http.createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/livez") {
      send(response, 200, { supervisor: "running" });
      return;
    }
    if (!(await authorized(request))) {
      send(response, 401, { error: "unauthorized" });
      return;
    }
    if (request.method === "GET" && request.url === "/status") {
      send(response, 200, publicStatus());
      return;
    }
    const body = await readBody(request);
    let result;
    if (request.method === "POST" && request.url === "/connect") result = await connect(body);
    else if (request.method === "POST" && request.url === "/pause") result = await pause();
    else if (request.method === "POST" && request.url === "/resume") result = await resume();
    else result = [404, { error: "not_found" }];
    send(response, result[0], result[1]);
  } catch (error) {
    send(response, 400, { error: "bad_request", detail: String(error.message || error) });
  }
});

async function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  clearTimeout(restartTimer);
  log("INFO", "stopping", { signal });
  await stopChild();
  await writeQueue.catch(() => {});
  server.close(() => process.exit(0));
}

await mkdir(STATE_DIR, { recursive: true });
await loadConnection();
server.listen(PORT, "0.0.0.0", async () => {
  const address = server.address();
  await publishStatus({ control_port: address.port });
  log("INFO", "control endpoint started", { port: address.port, simulated: status.simulated });
  if (connection && !status.paused) await startSync().catch(() => {});
});
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
