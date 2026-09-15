import { timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";

const DATA_DIR = process.env.COPPERMIND_DATA_DIR || "/data";
const NOTES_DIR = path.join(DATA_DIR, "notes");
const STATE_DIR = path.join(DATA_DIR, "state", "sync");
const STATUS_FILE = path.join(STATE_DIR, "status.json");
const CONNECTION_FILE = path.join(STATE_DIR, "connection.json");
const TOKEN_FILE =
  process.env.COPPERMIND_INTERNAL_TOKEN_FILE || "/run/coppermind/internal/internal-token";
const FAKE = process.env.COPPERMIND_SYNC_FAKE === "1";
const PORT = Number.parseInt(process.env.COPPERMIND_SYNC_PORT || "8092", 10);
const DEVICE_NAME = "Coppermind";
const PROBE_INTERVAL_MS = 30_000;
const RESTART_DELAY_MS = FAKE ? 250 : 2_000;
const COMMAND_TIMEOUT_MS = 10_000;

let child = null;
let restartTimer = null;
let heartbeatTimer = null;
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
  vault_name: null,
  device_name: DEVICE_NAME,
  mode: FAKE ? "fake" : "real",
  sync_mode: "bidirectional",
  conflict_strategy: "merge",
  cli_version: "0.0.14",
  last_sync_at: null,
  last_error: null,
  plan_limits_applied: false,
  sync_pid: null,
  control_port: null,
};

function log(event, fields = {}) {
  process.stdout.write(
    `${JSON.stringify({
      timestamp: new Date().toISOString(),
      level: "info",
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
    Object.assign(status, {
      configured: true,
      paused: value.paused === true,
      vault_name: value.vault_name,
      state: value.paused === true ? "paused" : "starting",
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

function run(command, args, timeoutMs = COMMAND_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const proc = spawn(command, args, { env: process.env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      proc.kill("SIGKILL");
      reject(new Error(`${command} timed out`));
    }, timeoutMs);
    proc.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    proc.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    proc.once("exit", (code, signal) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        reject(new Error(stderr.trim() || `${command} exited ${code ?? signal}`));
      }
    });
  });
}

function parseLastSync(raw) {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed.last_sync_at || parsed.lastSyncAt || parsed.lastSync || null;
  } catch {
    return null;
  }
}

async function probe() {
  if (FAKE) return new Date().toISOString();
  const raw = await run("ob", ["sync-status", "--path", NOTES_DIR, "--json"]);
  return parseLastSync(raw);
}

function syncCommand() {
  if (FAKE) return [process.execPath, [path.join(import.meta.dirname, "fake", "sync.mjs")]];
  return ["ob", ["sync", "--path", NOTES_DIR, "--continuous"]];
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
  await publishStatus({ state: "starting", connected: false, syncing: false, sync_pid: null });
  try {
    const lastSync = await probe();
    const [command, args] = syncCommand();
    const proc = spawn(command, args, { env: process.env, stdio: ["ignore", "pipe", "pipe"] });
    child = proc;
    proc.stdout.on("data", (chunk) => log("sync output", { output: chunk.toString().trim() }));
    proc.stderr.on("data", (chunk) => log("sync error output", { output: chunk.toString().trim() }));
    proc.once("error", (error) => handleChildExit(proc, null, null, error));
    proc.once("exit", (code, signal) => handleChildExit(proc, code, signal));
    await publishStatus({
      state: "syncing",
      connected: true,
      syncing: true,
      sync_pid: proc.pid,
      last_sync_at: lastSync || status.last_sync_at,
      last_error: null,
    });
    log("sync process started", { pid: proc.pid, mode: status.mode });
  } catch (error) {
    await publishStatus({
      state: "error",
      connected: false,
      syncing: false,
      sync_pid: null,
      last_error: String(error.message || error),
    });
    scheduleRestart();
    throw error;
  }
}

function scheduleRestart() {
  if (!connection || status.paused || stopping) return;
  clearTimeout(restartTimer);
  restartTimer = setTimeout(() => startSync().catch(() => {}), RESTART_DELAY_MS);
}

async function handleChildExit(proc, code, signal, error = null) {
  if (child !== proc) return;
  child = null;
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
    log("sync process stopped unexpectedly", { code, signal });
    scheduleRestart();
  }
}

async function checkLiveness() {
  if (!child || status.paused) return;
  try {
    const lastSync = await probe();
    await publishStatus({
      state: "syncing",
      connected: true,
      syncing: true,
      last_sync_at: lastSync || status.last_sync_at,
      last_error: null,
    });
  } catch (error) {
    await publishStatus({
      state: "stalled",
      connected: false,
      syncing: false,
      last_error: `sync status probe failed: ${String(error.message || error)}`,
    });
    await stopChild();
  }
}

async function connect(body) {
  if (child && status.syncing) return [409, { error: "already_connected" }];
  let vaultName = body.vault_name;
  if (FAKE) {
    vaultName = vaultName || "Fake remote vault";
  } else {
    if (body.email || body.password || body.mfa) {
      return [
        422,
        { error: "credentials_not_accepted", detail: "run ob login interactively first" },
      ];
    }
    if (typeof vaultName !== "string" || vaultName.length === 0) {
      return [422, { error: "vault_name_required" }];
    }
    try {
      await run("ob", ["sync-list-remote", "--json"]);
      await run("ob", [
        "sync-setup",
        "--vault",
        vaultName,
        "--path",
        NOTES_DIR,
        "--device-name",
        DEVICE_NAME,
        "--json",
      ]);
      await run("ob", [
        "sync-config",
        "--path",
        NOTES_DIR,
        "--mode",
        "bidirectional",
        "--conflict-strategy",
        "merge",
        "--configs",
        "",
        "--json",
      ]);
    } catch (error) {
      await publishStatus({
        state: "not_connected",
        connected: false,
        configured: false,
        syncing: false,
        last_error: String(error.message || error),
      });
      return [400, { error: "connect_failed", detail: status.last_error }];
    }
  }
  connection = { vault_name: vaultName, paused: false };
  await saveConnection();
  await publishStatus({ configured: true, paused: false, vault_name: vaultName });
  try {
    await startSync();
  } catch {
    return [502, { error: "sync_start_failed", detail: status.last_error }];
  }
  return [200, publicStatus()];
}

async function pause() {
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
  if (!connection) return [409, { error: "not_configured" }];
  connection.paused = false;
  status.paused = false;
  await saveConnection();
  try {
    await startSync();
  } catch {
    return [502, { error: "sync_start_failed", detail: status.last_error }];
  }
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
    if (request.method === "GET" && request.url === "/healthz") {
      send(response, status.syncing ? 200 : 503, {
        healthy: status.syncing,
        state: status.state,
      });
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
  clearInterval(heartbeatTimer);
  log("stopping", { signal });
  await stopChild();
  server.close(() => process.exit(0));
}

await mkdir(NOTES_DIR, { recursive: true });
await mkdir(STATE_DIR, { recursive: true });
await loadConnection();
server.listen(PORT, "0.0.0.0", async () => {
  const address = server.address();
  await publishStatus({ control_port: address.port });
  log("control endpoint started", { port: address.port, mode: status.mode });
  if (connection && !status.paused) await startSync().catch(() => {});
});
heartbeatTimer = setInterval(async () => {
  await checkLiveness();
  await publishStatus();
}, PROBE_INTERVAL_MS);
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
