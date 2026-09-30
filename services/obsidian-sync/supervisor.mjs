import { timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";

import { deviceName } from "./settings.mjs";

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
// Bound restart storms while retaining automatic recovery.
const RESTART_BASE_MS = 250;
const RESTART_CEILING_MS = 2_000;
const RESTART_STABLE_MS = 2_000;

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
  configured: false,
  syncing: false,
  paused: false,
  // True whenever the supervised child is the bundled simulator rather than
  // the Obsidian client. Nothing in a simulated run reaches a remote vault.
  simulated: FAKE,
  real_sync_supported: true,
  device_name: null,
  last_sync_at: null,
  client_status: null,
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
    connection = {
      vault_name: value.vault_name,
      vault_id: value.vault_id,
      device_name: value.device_name,
      paused: value.paused === true,
    };
    const paused = value.paused === true;
    Object.assign(status, {
      configured: true,
      paused,
      vault_name: value.vault_name,
      device_name: value.device_name,
      state: paused ? "paused" : "starting",
      last_error: null,
    });
  } catch (error) {
    if (error.code !== "ENOENT") {
      Object.assign(status, { state: "error", last_error: "Cannot read sync connection" });
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
  await publishStatus({ state: "starting", syncing: false, sync_pid: null });
  const proc = FAKE
    ? spawn(process.execPath, [path.join(import.meta.dirname, "fake", "sync.mjs")], {
        stdio: ["ignore", "pipe", "ignore"],
      })
    : spawn("ob", ["sync", "--path", path.join(DATA_DIR, "notes"), "--continuous"], {
        stdio: "ignore",
      });
  child = proc;
  childStartedAt = Date.now();
  // Real client output is never forwarded. Login prints the account email.
  // The simulator emits only its fixed configuration report.
  if (FAKE) proc.stdout.on("data", (chunk) => {
    for (const line of chunk.toString().split("\n")) observeClientReport(line);
  });
  proc.once("error", (error) => handleChildExit(proc, null, null, error));
  proc.once("exit", (code, signal) => handleChildExit(proc, code, signal));
  await publishStatus({
    state: "syncing",
    syncing: true,
    sync_pid: proc.pid,
    last_error: null,
  });
  log("INFO", "sync process started", { pid: proc.pid, simulated: status.simulated });
}

function scheduleRestart() {
  if (!connection || status.paused || stopping) return;
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
    ? "sync process could not start"
    : `sync process exited (${code ?? signal ?? "unknown"})`;
  await publishStatus({
    state: expected ? (status.paused ? "paused" : "stopping") : "restarting",
    syncing: false,
    sync_pid: null,
    last_error: expected ? null : message,
  });
  if (!expected) {
    log("WARNING", "sync process stopped unexpectedly", { code, signal });
    scheduleRestart();
  }
}

// Short-lived command output stays in memory. No argv, stdout, stderr or
// exception object is logged. Failures publish a scrubbed, bounded detail.
async function command(args, secrets = [], json = false, accepted = [0]) {
  return new Promise((resolve, reject) => {
    const proc = spawn("ob", args, { stdio: ["ignore", "pipe", "pipe"] });
    let out = "", err = "", oversized = false;
    const timer = setTimeout(() => proc.kill("SIGKILL"), 60_000);
    const collect = (chunk, stderr) => {
      if (out.length + err.length + chunk.length > 262144) {
        oversized = true;
        proc.kill("SIGKILL");
      } else if (stderr) err += chunk; else out += chunk;
    };
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");
    proc.stdout.on("data", (chunk) => collect(chunk, false));
    proc.stderr.on("data", (chunk) => collect(chunk, true));
    proc.once("error", () => { clearTimeout(timer); reject(new Error("Obsidian client unavailable")); });
    proc.once("close", (code) => {
      clearTimeout(timer);
      if (!accepted.includes(code) || oversized) {
        for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) {
          err = err.split(secret).join("[redacted]");
          err = err.split(JSON.stringify(secret).slice(1, -1)).join("[redacted]");
        }
        // Only setup has a request-scoped redaction list. Boot and other
        // controls retain a generic error rather than arbitrary client prose.
        const detail = secrets.length ? err.trim().slice(0, 1500) : "";
        reject(new Error(`Obsidian ${args[0]} failed. Check credentials, MFA and connectivity.${detail ? ` ${detail}` : ""}`));
      } else if (json) {
        try { resolve(JSON.parse(out)); }
        catch { reject(new Error("Obsidian client returned invalid JSON")); }
      } else resolve(null);
    });
  });
}

async function refreshClientStatus(secrets = []) {
  if (FAKE || !connection) return;
  try {
    let report = await command(["sync-status", "--path", path.join(DATA_DIR, "notes"), "--json"], secrets, true);
    let serialized = JSON.stringify(report);
    for (const secret of secrets) {
      serialized = serialized.split(JSON.stringify(secret).slice(1, -1)).join("[redacted]");
    }
    report = JSON.parse(serialized);
    // 0.0.14 reports configuration, not delivery timestamps or credentials.
    const safe = {};
    for (const key of ["vaultId", "vaultName", "vaultPath", "syncMode", "conflictStrategy", "deviceName", "configDir", "fileTypes", "configs", "excludedFolders"]) {
      if (key in report) safe[key] = report[key];
    }
    await publishStatus({ client_status: safe, sync_mode: safe.syncMode ?? null,
      conflict_strategy: safe.conflictStrategy ?? null });
  } catch {
    await publishStatus({ last_error: "Obsidian sync status unavailable" });
  }
}

async function connect(body) {
  const vaultName = body?.vault_name;
  if (typeof vaultName !== "string" || !vaultName.trim()) return [400, { error: "vault_name_required" }];
  for (const key of ["email", "password", "encryption_password"]) {
    if (typeof body[key] !== "string" || !body[key]) return [400, { error: "credentials_required" }];
  }
  if (typeof body.existing_vault !== "boolean" ||
      (body.mfa_code != null && typeof body.mfa_code !== "string")) return [400, { error: "invalid_connect" }];
  if (connection) return [409, { error: "already_connected" }];
  const secrets = [body.email, body.password, body.mfa_code, body.encryption_password].filter(Boolean);
  try {
    const device = await deviceName(path.join(DATA_DIR, "state", "settings.yaml"));
    if (body.device_name != null && body.device_name !== device) {
      return [409, { error: "device_setting_changed" }];
    }
    // These names are durable. Refuse overlapping input before writing any
    // settings, client configuration, or connection metadata.
    if ([vaultName, device].some((value) => secrets.some((secret) => value.includes(secret)))) {
      return [400, { error: "names_must_not_contain_credentials" }];
    }
    let vaultId = "simulated";
    if (!FAKE) {
      const login = ["login", "--email", body.email, "--password", body.password];
      if (body.mfa_code) login.push("--mfa", body.mfa_code);
      await command(login, secrets);
      if (!body.existing_vault) await command(["sync-create-remote", "--name", vaultName,
        "--encryption", "end-to-end", "--password", body.encryption_password], secrets);
      const remote = await command(["sync-list-remote", "--json"], secrets, true);
      const matches = [...remote.vaults, ...remote.shared].filter((v) => v.name === vaultName);
      if (matches.length !== 1) throw new Error("Remote vault name is absent or ambiguous. Use a unique name and join an existing vault after a creation retry.");
      vaultId = matches[0].id;
      if (typeof vaultId !== "string" || !vaultId || secrets.some((secret) => vaultId.includes(secret))) {
        throw new Error("Obsidian client returned an invalid remote vault ID");
      }
      await command(["sync-setup", "--vault", vaultId, "--path", path.join(DATA_DIR, "notes"),
        "--password", body.encryption_password, "--device-name", device, "--json"], secrets, true);
    }
    const next = { vault_name: vaultName, vault_id: vaultId, device_name: device, paused: false };
    await writeJsonAtomically(CONNECTION_FILE, next);
    connection = next;
    restartAttempts = 0;
    await publishStatus({ configured: true, paused: false, vault_name: vaultName, device_name: device });
    await startSync();
    await refreshClientStatus(secrets);
    return [200, publicStatus()];
  } catch (error) {
    const detail = error.message.startsWith("Remote vault name") || error.message.startsWith("Obsidian ")
      ? error.message : "Sync setup failed. Check settings and retry.";
    await publishStatus({ last_error: detail });
    return [502, { error: "connect_failed", detail }];
  }
}

async function disconnect() {
  clearTimeout(restartTimer);
  status.paused = true;
  if (connection) { connection.paused = true; await saveConnection(); }
  await stopChild();
  if (!FAKE) {
    // A failed setup can leave an account token without a local connection.
    let unlinkError;
    try { await command(["sync-unlink", "--path", path.join(DATA_DIR, "notes")], [], false, [0, 3]); }
    catch (error) { if (connection) unlinkError = error; }
    await command(["logout"]);
    if (unlinkError) throw unlinkError;
  }
  await rm(CONNECTION_FILE, { force: true });
  connection = null;
  await publishStatus({ state: "not_connected", configured: false, paused: false,
    syncing: false, sync_pid: null, vault_name: null, device_name: null,
    client_status: null, sync_mode: null, conflict_strategy: null, last_sync_at: null, last_error: null });
  return [200, publicStatus()];
}

async function pause() {
  if (!connection) return [409, { error: "not_configured" }];
  connection.paused = true;
  status.paused = true;
  clearTimeout(restartTimer);
  await saveConnection();
  await stopChild();
  await publishStatus({ state: "paused", syncing: false, sync_pid: null });
  return [200, publicStatus()];
}

async function resume() {
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

let controlBusy = false;
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
    if (controlBusy) { send(response, 409, { error: "control_busy" }); return; }
    controlBusy = true;
    try {
      const body = await readBody(request);
      let result;
      if (request.method === "POST" && request.url === "/connect") result = await connect(body);
      else if (request.method === "POST" && request.url === "/pause") result = await pause();
      else if (request.method === "POST" && request.url === "/resume") result = await resume();
      else if (request.method === "POST" && request.url === "/disconnect") result = await disconnect();
      else result = [404, { error: "not_found" }];
      send(response, result[0], result[1]);
    } finally { controlBusy = false; }
  } catch {
    await publishStatus({ last_error: "Sync request failed" }).catch(() => {});
    send(response, 400, { error: "bad_request", detail: "Sync request failed" });
  }
});

async function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  clearTimeout(restartTimer);
  log("INFO", "stopping", { signal });
  await stopChild();
  await publishStatus({
    state: "stopped",
    syncing: false,
    sync_pid: null,
    control_port: null,
  });
  server.close(() => process.exit(0));
}

await mkdir(STATE_DIR, { recursive: true });
await loadConnection();
server.listen(PORT, "0.0.0.0", async () => {
  const address = server.address();
  await publishStatus({ control_port: address.port });
  log("INFO", "control endpoint started", { port: address.port, simulated: status.simulated });
  if (connection && !status.paused) await startSync().catch(() => {});
  await refreshClientStatus();
});
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
