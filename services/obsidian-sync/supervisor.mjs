import { timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";

import { deviceName, syncSettings, clientMatches } from "./settings.mjs";

const DATA_DIR = process.env.COPPERMIND_DATA_DIR || "/data";
const STATE_DIR = path.join(DATA_DIR, "state", "sync");
const STATUS_FILE = path.join(STATE_DIR, "status.json");
const CONNECTION_FILE = path.join(STATE_DIR, "connection.json");
const DEVICES_FILE = path.join(STATE_DIR, "devices.json");
// The simulator's stand-ins for what only a real account can supply. A test writes these.
const FAKE_SIGNED_OUT = path.join(STATE_DIR, "fake-signed-out");
const FAKE_VAULTS = path.join(STATE_DIR, "fake-vaults.json");
const FAKE_DEVICES = path.join(STATE_DIR, "fake-devices.json");
// Which Obsidian account this server signed in with, so signing in again asks only for the
// password. It sits beside the account token on the credential volume: never under /data,
// never in a status file or a log. Where that volume is absent nothing is kept.
const CREDENTIALS_DIR = process.env.COPPERMIND_SYNC_CREDENTIALS_DIR || "/var/lib/obsidian-sync";
const ACCOUNT_FILE = path.join(CREDENTIALS_DIR, "coppermind-account");
// A sign-in that has not been turned into a connection is forgotten after this long.
const PENDING_MS = 30 * 60 * 1000;
// This many quick exits in a row and the client is asked whether it is still signed in.
const SIGNIN_CHECK_AFTER = 3;
const DEVICES_EVERY_MS = 60_000;
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
// Between signing in and connecting: the account, its vaults, and the vault chosen. Held in
// memory only, so a vault's own password is never written anywhere before it is used.
let pending = null;
let quickExits = 0;
let signinEpoch = 0;
let devicesReadAt = 0;
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
  config_live: false,
  config_matches: false,
  config_error: null,
  vault_name: null,
  // True when the client keeps stopping and is no longer signed in to Obsidian.
  needs_signin: false,
  // Whether signing in again needs only the password. Never the address itself.
  account_known: false,
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

async function knownAccount() {
  try { return (await readFile(ACCOUNT_FILE, "utf8")).trim(); }
  catch { return ""; }
}

async function rememberAccount(account) {
  if (typeof account !== "string" || !account || !existsSync(CREDENTIALS_DIR)) return;
  try {
    await writeFile(ACCOUNT_FILE, account + "\n", { mode: 0o600 });
    status.account_known = true;
  } catch { /* Not being able to keep it only means the address is asked for next time. */ }
}

async function forgetAccount() {
  await rm(ACCOUNT_FILE, { force: true }).catch(() => {});
  status.account_known = false;
}

async function loadConnection() {
  status.account_known = Boolean(await knownAccount());
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
  if (!connection || status.paused || stopping || child || status.needs_signin) return;
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

async function signedIn() {
  if (FAKE) return !existsSync(FAKE_SIGNED_OUT);
  try {
    // With no arguments the client reports the account it is signed in to, or fails.
    await command(["login"]);
    return true;
  } catch {
    return false;
  }
}

function scheduleRestart() {
  if (!connection || status.paused || stopping || status.needs_signin) return;
  clearTimeout(restartTimer);
  const delay = Math.min(RESTART_BASE_MS * 2 ** restartAttempts, RESTART_CEILING_MS);
  restartAttempts += 1;
  log("INFO", "sync restart scheduled", { attempt: restartAttempts, delay_ms: delay });
  restartTimer = setTimeout(() => startSync().catch(() => {}), delay);
}

async function handleChildExit(proc, code, signal, error = null) {
  if (child !== proc) return;
  child = null;
  const stable = Date.now() - childStartedAt >= RESTART_STABLE_MS;
  if (stable) restartAttempts = 0;
  quickExits = stable ? 0 : quickExits + 1;
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
    const epoch = signinEpoch;
    if (quickExits >= SIGNIN_CHECK_AFTER && !(await signedIn()) && epoch === signinEpoch) {
      // Restarting cannot help a client that is signed out. Say so and wait for a sign-in.
      clearTimeout(restartTimer);
      await publishStatus({ state: "error", needs_signin: true,
        last_error: "Obsidian is asking this server to sign in again" });
      log("WARNING", "sync stopped: the client is no longer signed in");
      return;
    }
    scheduleRestart();
  }
}

// Short-lived command output stays in memory. No argv, stdout, stderr or
// exception object is logged. Failures publish a scrubbed, bounded detail.
// What the client prints when the account wants a sign-in code as well as the password.
const ASKS_FOR_CODE = /2FA code/i;

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
        const failure = new Error(`Obsidian ${args[0]} failed. Check credentials, MFA and connectivity.${detail ? ` ${detail}` : ""}`);
        failure.mfa = ASKS_FOR_CODE.test(out + err);
        reject(failure);
      } else if (args[0] === "login" && ASKS_FOR_CODE.test(out + err)) {
        // The client asks for the code on a terminal it does not have and can still exit
        // cleanly, so the question itself is the sign that a code is needed.
        const failure = new Error("Obsidian login failed. A sign-in code is needed.");
        failure.mfa = true;
        reject(failure);
      } else if (json) {
        try { resolve(JSON.parse(out)); }
        catch { reject(new Error("Obsidian client returned invalid JSON")); }
      } else resolve(null);
    });
  });
}

async function refreshClientStatus(secrets = []) {
  if (!connection) return;
  if (FAKE) return;
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
    await publishStatus({ client_status: safe, config_live: true, sync_mode: safe.syncMode ?? null,
      conflict_strategy: safe.conflictStrategy ?? null });
  } catch {
    await publishStatus({ client_status: null, config_live: false, config_matches: false,
      config_error: "Live sync configuration unavailable" });
  }
}

async function reconcileConfig() {
  if (!connection) return;
  try {
    const desired = await syncSettings(path.join(DATA_DIR, "state", "settings.yaml"));
    await refreshClientStatus();
    if (status.config_live && clientMatches(desired, status.client_status)) {
      await publishStatus({ config_matches: true, config_error: null });
      if (!status.paused) await startSync();
      return;
    }
    // Stop before changing client configuration. A failed apply leaves sync
    // stopped until a verified retry, preserving the operator's paused choice.
    clearTimeout(restartTimer);
    const wasPaused = status.paused;
    status.paused = true;
    await stopChild();
    status.paused = wasPaused;
    await publishStatus({ state: "configuring", syncing: false, config_matches: false });
    if (!FAKE) {
      await command(["sync-config", "--path", path.join(DATA_DIR, "notes"),
        "--device-name", desired.device_name, "--mode", desired.mode,
        "--conflict-strategy", desired.conflict_strategy,
        "--file-types", desired.file_types.join(","), "--configs", desired.sync_configs.join(","),
        "--excluded-folders", desired.excluded_folders.join(","), "--json"], [], true);
      await refreshClientStatus();
    } else {
      await publishStatus({ config_live: true, client_status: {
        deviceName: desired.device_name, syncMode: desired.mode, conflictStrategy: desired.conflict_strategy,
        fileTypes: desired.file_types, configs: desired.sync_configs, excludedFolders: desired.excluded_folders,
        configDir: ".obsidian",
      } });
    }
    if (!status.config_live || !clientMatches(desired, status.client_status)) {
      throw new Error("Configuration did not match after apply");
    }
    connection.device_name = desired.device_name;
    await saveConnection();
    await publishStatus({ config_matches: true, config_error: null, device_name: desired.device_name,
      state: status.paused ? "paused" : "starting" });
    if (!status.paused) await startSync();
  } catch {
    await publishStatus({ config_matches: false, config_error: "Sync settings could not be applied and verified. Retrying automatically." });
  }
}

function pendingView() {
  if (pending && Date.now() - pending.at > PENDING_MS) pending = null;
  if (!pending) return { signed_in: false, vaults: [], choice: null };
  return {
    signed_in: true,
    vaults: pending.vaults.map(({ id, name, region, shared, locked }) => ({ id, name, region, shared, locked })),
    choice: pending.choice
      ? { vault_id: pending.choice.vault_id, vault_name: pending.choice.vault_name, new_vault: pending.choice.new_vault }
      : null,
  };
}

async function fakeVaults() {
  try { return JSON.parse(await readFile(FAKE_VAULTS, "utf8")); }
  catch {
    return [
      { id: "fake-1", name: "My notes", region: "simulated", locked: true, password: "vault-password" },
      { id: "fake-2", name: "Work archive", region: "simulated", locked: false },
    ];
  }
}

// Whether a vault has a password of its own, found by asking the client to set it up in a
// throwaway folder with none: a vault without one is set up and then unlinked again, and a
// locked one refuses. `password` checks that password against the vault the same way.
// Returns true (locked, or the password was wrong), false, or null when it cannot tell.
async function refusesWithout(vaultId, secrets, password = null) {
  const scratch = await mkdtemp(path.join(os.tmpdir(), "coppermind-vault-"));
  try {
    const args = ["sync-setup", "--vault", vaultId, "--path", scratch, "--json"];
    if (password) args.push("--password", password);
    try {
      await command(args, secrets, true);
    } catch (error) {
      if (/Password not provided|Failed to validate password/.test(error.message)) return true;
      return null;
    }
    await command(["sync-unlink", "--path", scratch], [], false, [0, 3]).catch(() => {});
    return false;
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
}

async function login(body) {
  if (connection) return [409, { error: "already_connected" }];
  for (const key of ["email", "password"]) {
    if (typeof body?.[key] !== "string" || !body[key]) return [400, { error: "credentials_required" }];
  }
  if (body.mfa_code != null && typeof body.mfa_code !== "string") return [400, { error: "invalid_login" }];
  const secrets = [body.email, body.password, body.mfa_code].filter(Boolean);
  try {
    let vaults;
    if (FAKE) {
      if (body.password === "wrong") throw new Error("Obsidian login failed. Check credentials, MFA and connectivity.");
      if (body.password === "mfa" && !body.mfa_code) throw Object.assign(new Error("Obsidian login failed. A sign-in code is needed."), { mfa: true });
      vaults = (await fakeVaults()).map((v) => ({ shared: false, ...v }));
    } else {
      const args = ["login", "--email", body.email, "--password", body.password];
      if (body.mfa_code) args.push("--mfa", body.mfa_code);
      await command(args, secrets);
      const remote = await command(["sync-list-remote", "--json"], secrets, true);
      vaults = [
        ...remote.vaults.map((v) => ({ ...v, shared: false })),
        ...remote.shared.map((v) => ({ ...v, shared: true })),
      ];
      for (const vault of vaults) vault.locked = await refusesWithout(vault.id, secrets);
    }
    pending = { account: body.email, vaults, choice: null, at: Date.now() };
    await publishStatus({ last_error: null });
    return [200, { ...publicStatus() }];
  } catch (error) {
    const detail = error.message.startsWith("Obsidian ") ? error.message : "Sign-in failed.";
    return [502, { error: "login_failed", mfa_needed: error.mfa === true, detail }];
  }
}

async function chooseVault(body) {
  pendingView();
  if (!pending) return [409, { error: "not_signed_in" }];
  if (body?.clear === true) { pending.choice = null; return [200, publicStatus()]; }
  if (typeof body?.new_name === "string" && body.new_name.trim()) {
    const name = body.new_name.trim();
    if (pending.vaults.some((v) => v.name === name)) return [400, { error: "name_taken" }];
    if (name.includes(pending.account)) return [400, { error: "names_must_not_contain_credentials" }];
    pending.choice = { vault_id: null, vault_name: name, new_vault: true, password: null };
    return [200, publicStatus()];
  }
  const vault = pending.vaults.find((v) => v.id === body?.vault_id);
  if (!vault) return [400, { error: "vault_required" }];
  const password = typeof body.password === "string" && body.password ? body.password : null;
  if (vault.locked !== false) {
    if (!password && vault.locked === true) return [400, { error: "password_required" }];
    if (password) {
      const wrong = FAKE ? password !== vault.password : await refusesWithout(vault.id, [password, pending.account], password);
      if (wrong !== false) return [400, { error: "wrong_password" }];
    }
  }
  pending.choice = { vault_id: vault.id, vault_name: vault.name, new_vault: false, password };
  return [200, publicStatus()];
}

async function forgetLogin() {
  pending = null;
  if (!FAKE && !connection) await command(["logout"]).catch(() => {});
  return [200, publicStatus()];
}

async function connectChosen(body) {
  pendingView();
  if (!pending?.choice) return [409, { error: "vault_not_chosen" }];
  const choice = pending.choice;
  const secrets = [pending.account, choice.password].filter(Boolean);
  try {
    const device = await deviceName(path.join(DATA_DIR, "state", "settings.yaml"));
    if (body?.device_name != null && body.device_name !== device) {
      return [409, { error: "device_setting_changed" }];
    }
    let vaultId = choice.vault_id ?? "simulated";
    if (!FAKE) {
      if (choice.new_vault) {
        await command(["sync-create-remote", "--name", choice.vault_name, "--encryption", "standard"], secrets);
        const remote = await command(["sync-list-remote", "--json"], secrets, true);
        const matches = [...remote.vaults, ...remote.shared].filter((v) => v.name === choice.vault_name);
        if (matches.length !== 1) throw new Error("Obsidian did not list the new vault under that name.");
        vaultId = matches[0].id;
      }
      const setup = ["sync-setup", "--vault", vaultId, "--path", path.join(DATA_DIR, "notes"),
        "--device-name", device, "--json"];
      if (choice.password) setup.push("--password", choice.password);
      await command(setup, secrets, true);
    }
    const next = { vault_name: choice.vault_name, vault_id: vaultId, device_name: device, paused: false };
    await writeJsonAtomically(CONNECTION_FILE, next);
    connection = next;
    await rememberAccount(pending.account);
    pending = null;
    restartAttempts = 0;
    quickExits = 0;
    signinEpoch += 1;
    await publishStatus({ configured: true, paused: false, vault_name: next.vault_name,
      device_name: device, needs_signin: false });
    await reconcileConfig();
    return [status.config_matches ? 200 : 502, publicStatus()];
  } catch (error) {
    const detail = error.message.startsWith("Obsidian ") ? error.message : "Sync setup failed. Check settings and retry.";
    await publishStatus({ last_error: detail });
    return [502, { error: "connect_failed", detail }];
  }
}

// Sign in again after Obsidian has signed this server out. The vault stays connected. Only
// the password is needed: the account is the one this server connected with. A server that
// connected before the account was kept is asked for it once.
async function signinAgain(body) {
  if (!connection) return [409, { error: "not_configured" }];
  const given = typeof body?.email === "string" ? body.email : "";
  const account = given || await knownAccount();
  if (!account) return [400, { error: "account_required" }];
  if (typeof body?.password !== "string" || !body.password) {
    return [400, { error: "credentials_required" }];
  }
  const secrets = [account, body.password, body.mfa_code].filter(Boolean);
  try {
    if (FAKE) {
      if (body.password === "wrong") throw new Error("Obsidian login failed. Check credentials, MFA and connectivity.");
      await rm(FAKE_SIGNED_OUT, { force: true });
    } else {
      const args = ["login", "--email", account, "--password", body.password];
      if (body.mfa_code) args.push("--mfa", body.mfa_code);
      await command(args, secrets);
    }
  } catch (error) {
    const detail = error.message.startsWith("Obsidian ") ? error.message : "Sign-in failed.";
    return [502, { error: "login_failed", mfa_needed: error.mfa === true, detail }];
  }
  await rememberAccount(account);
  restartAttempts = 0;
  quickExits = 0;
  signinEpoch += 1;
  await publishStatus({ needs_signin: false, last_error: null });
  await reconcileConfig();
  return [200, publicStatus()];
}

// Which devices changed a note last, and how much the vault holds, read from the client's
// own record of the files it has seen. Nothing here is asked of Obsidian.
async function readDevices() {
  if (FAKE) {
    try { return JSON.parse(await readFile(FAKE_DEVICES, "utf8")); }
    catch { return { devices: [], last_change: null, used_bytes: null, last_activity_at: null }; }
  }
  const home = process.env.XDG_CONFIG_HOME || path.join(process.env.HOME || "", ".config");
  const file = path.join(home, "obsidian-headless", "sync", String(connection.vault_id), "state.db");
  const { DatabaseSync } = await import("node:sqlite");
  const database = new DatabaseSync(file, { readOnly: true });
  try {
    const devices = new Map();
    let used = 0;
    let last = null;
    for (const row of database.prepare("SELECT data FROM server_files").iterate()) {
      let entry;
      try { entry = JSON.parse(row.data); } catch { continue; }
      if (!entry || entry.folder) continue;
      if (!entry.deleted && Number.isFinite(entry.size)) used += entry.size;
      const name = typeof entry.device === "string" ? entry.device.trim() : "";
      if (!name || name === connection.device_name || !Number.isFinite(entry.mtime)) continue;
      const seen = devices.get(name);
      if (!seen || entry.mtime > seen.mtime) devices.set(name, { mtime: entry.mtime, path: entry.path });
      if (!last || entry.mtime > last.mtime) last = { mtime: entry.mtime, path: entry.path, device: name };
    }
    const at = (ms) => new Date(ms).toISOString();
    return {
      devices: [...devices].map(([name, seen]) => ({ name, last_change_at: at(seen.mtime), path: seen.path }))
        .sort((a, b) => (a.last_change_at < b.last_change_at ? 1 : -1)),
      last_change: last ? { at: at(last.mtime), device: last.device, path: last.path } : null,
      used_bytes: used,
      last_activity_at: at((await stat(file)).mtimeMs),
    };
  } finally {
    database.close();
  }
}

async function publishDevices(force = false) {
  if (!connection) { await rm(DEVICES_FILE, { force: true }); return; }
  if (!force && Date.now() - devicesReadAt < DEVICES_EVERY_MS) return;
  devicesReadAt = Date.now();
  try { await writeJsonAtomically(DEVICES_FILE, await readDevices()); }
  catch { /* The client has not synced yet, or its record cannot be read. Nothing is claimed. */ }
}

async function connect(body) {
  // A connection made in steps (sign in, pick a vault, connect) carries no credentials here.
  if (pending?.choice && body?.email == null && body?.password == null) return connectChosen(body);
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
    await rememberAccount(body.email);
    pending = null;
    restartAttempts = 0;
    quickExits = 0;
    signinEpoch += 1;
    await publishStatus({ configured: true, paused: false, vault_name: vaultName, device_name: device,
      needs_signin: false });
    await reconcileConfig();
    return [status.config_matches ? 200 : 502, publicStatus()];
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
  await rm(DEVICES_FILE, { force: true });
  await forgetAccount();
  connection = null;
  pending = null;
  quickExits = 0;
  await publishStatus({ state: "not_connected", configured: false, paused: false,
    syncing: false, sync_pid: null, vault_name: null, device_name: null,
    needs_signin: false,
    client_status: null, config_live: false, config_matches: false, config_error: null,
    sync_mode: null, conflict_strategy: null, last_sync_at: null, last_error: null });
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
  await reconcileConfig();
  return [status.config_matches ? 200 : 502, publicStatus()];
}

function publicStatus() {
  return { ...status, ...pendingView() };
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

let controlBusy = true;
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

    if (controlBusy) {
      if (request.method === "GET" && request.url === "/status") {
        // Keep connection information available without presenting cached
        // client values as freshly verified while a control owns the client.
        send(response, 200, { ...publicStatus(), control_busy: true,
          config_live: false, config_matches: false });
      } else send(response, 409, { error: "control_busy" });
      return;
    }
    controlBusy = true;
    try {
      if (request.method === "GET" && request.url === "/status") {
        await refreshClientStatus();
        if (connection) {
          const desired = await syncSettings(path.join(DATA_DIR, "state", "settings.yaml"));
          await publishStatus({ config_matches: status.config_live && clientMatches(desired, status.client_status) });
        }
        send(response, 200, publicStatus());
        return;
      }
      const body = await readBody(request);
      let result;
      if (request.method === "POST" && request.url === "/connect") result = await connect(body);
      else if (request.method === "POST" && request.url === "/login") result = await login(body);
      else if (request.method === "POST" && request.url === "/vault") result = await chooseVault(body);
      else if (request.method === "POST" && request.url === "/forget-login") result = await forgetLogin();
      else if (request.method === "POST" && request.url === "/signin") result = await signinAgain(body);
      else if (request.method === "POST" && request.url === "/configure") {
        await reconcileConfig();
        result = [!connection || status.config_matches ? 200 : 502, publicStatus()];
      }
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
  try { await reconcileConfig(); await publishDevices(true); } finally { controlBusy = false; }
  const timer = setInterval(async () => {
    if (controlBusy || stopping) return;
    controlBusy = true;
    try { await reconcileConfig(); await publishDevices(); } finally { controlBusy = false; }
  }, 15_000);
  timer.unref();
});
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
