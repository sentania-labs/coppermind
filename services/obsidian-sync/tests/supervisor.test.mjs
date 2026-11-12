import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readdir, readFile, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { deviceName, DEFAULT_DEVICE_NAME } from "../settings.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const form = { email: "operator@example.invalid", password: "account-test-secret", mfa_code: "482916",
  encryption_password: "encryption-test-secret", vault_name: "Phone notes", existing_vault: false };
const secrets = [form.email, form.password, form.mfa_code, form.encryption_password];

async function waitFor(check, message, attempts = 150) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try { const value = await check(); if (value) return value; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  throw new Error(message);
}

async function harness(context, { fake = false, persisted = false, failure = "", level = "INFO", remoteNames = ["Phone notes"] } = {}) {
  const data = await mkdtemp(path.join(os.tmpdir(), "coppermind-sync-"));
  const stateDir = path.join(data, "state", "sync");
  await mkdir(stateDir, { recursive: true });
  const tokenFile = path.join(data, "internal-token");
  await writeFile(tokenFile, "test-control-token\n");
  await writeFile(path.join(data, "state", "settings.yaml"), "sync:\n  device_name: 'Kitchen server'\nother_section:\n  ignored: true\n");
  const connectionFile = path.join(stateDir, "connection.json");
  if (persisted) await writeFile(connectionFile, JSON.stringify({ vault_name: form.vault_name,
    vault_id: "remote-1", device_name: "Kitchen server", paused: persisted === "paused" }));
  const bin = path.join(data, "bin");
  await mkdir(bin);
  // Stands in for the credential volume, which is not under the data folder in a real install.
  const credentials = path.join(data, "credentials");
  await mkdir(credentials);
  const callsFile = path.join(data, "calls.jsonl");
  // Test-only argv capture intentionally contains synthetic secrets. Production
  // never records argv. Split stderr checks redaction across stream chunks.
  await writeFile(path.join(bin, "ob"), `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
if (args[0] === 'sync-status') {
  const block = ${JSON.stringify(path.join(data, 'block-status'))};
  if (fs.existsSync(block)) {
    fs.writeFileSync(${JSON.stringify(path.join(data, 'status-blocked'))}, 'yes');
    while (fs.existsSync(block)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
  }
}
fs.appendFileSync(${JSON.stringify(callsFile)}, JSON.stringify(args) + '\\n');
if (args[0] === ${JSON.stringify(failure)}) {
  process.stderr.write(${JSON.stringify(secrets.join(" ").slice(0, 10))});
  setTimeout(() => { process.stderr.write(${JSON.stringify(secrets.join(" ").slice(10))}); process.exit(2); }, 10);
} else if (args[0] === 'sync-list-remote') console.log(JSON.stringify({vaults:${JSON.stringify(remoteNames.map((name) => ({ id: 'remote-1', name })))},shared:[]}));
else if (args[0] === 'sync-config') {
  const config = {deviceName:args[args.indexOf('--device-name')+1],syncMode:args[args.indexOf('--mode')+1],conflictStrategy:args[args.indexOf('--conflict-strategy')+1],configDir:'.obsidian'};
  for (const [key, flag] of [['fileTypes','--file-types'],['configs','--configs'],['excludedFolders','--excluded-folders']]) config[key]=args[args.indexOf(flag)+1].split(',').filter(Boolean);
  fs.writeFileSync(${JSON.stringify(path.join(data, 'client.json'))},JSON.stringify(config));
  console.log('{}');
}
else if (args[0] === 'sync-status') {
  try { console.log(fs.readFileSync(${JSON.stringify(path.join(data, 'client.json'))},'utf8')); }
  catch { console.log(JSON.stringify({vaultId:'remote-1',vaultName:'Phone notes',syncMode:'bidirectional',conflictStrategy:'merge',deviceName:'Kitchen server'})); }
}
else if (args[0] === 'sync-setup' && fs.existsSync(${JSON.stringify(path.join(data, 'vault-locked'))})) {
  const at = args.indexOf('--password');
  if (at < 0) { process.stderr.write('Password not provided'); process.exit(1); }
  if (args[at + 1] !== 'vault-secret') { process.stderr.write('Failed to validate password'); process.exit(1); }
  console.log('{}');
}
else if (args[0] === 'sync-setup') console.log('{}');
else if (args[0] === 'login') {
  const signedOut = ${JSON.stringify(path.join(data, 'signed-out'))};
  const hold = ${JSON.stringify(path.join(data, 'login-hold'))};
  while (!args.includes('--password') && fs.existsSync(hold)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
  if (args.includes('--password')) fs.rmSync(signedOut, { force: true });
  else if (fs.existsSync(signedOut)) process.exit(1);
  console.log(${JSON.stringify(secrets.join(' '))});
}
else if (args[0] === 'sync' && fs.existsSync(${JSON.stringify(path.join(data, 'sync-exits'))})) process.exit(1);
else if (args[0] === 'sync') { console.log(${JSON.stringify(secrets.join(' '))}); console.error(${JSON.stringify(secrets.join(' '))}); setInterval(() => {}, 1000); }
`, { mode: 0o755 });
  let log = "";
  const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
    env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, COPPERMIND_DATA_DIR: data,
      COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile, COPPERMIND_SYNC_FAKE: fake ? "1" : "0",
      COPPERMIND_SYNC_PORT: "0", COPPERMIND_LOG_LEVEL: level,
      COPPERMIND_SYNC_CREDENTIALS_DIR: credentials },
    stdio: ["ignore", "pipe", "pipe"],
  });
  supervisor.stdout.on("data", (chunk) => { log += chunk; });
  supervisor.stderr.on("data", (chunk) => { log += chunk; });
  context.after(async () => {
    if (supervisor.exitCode == null && supervisor.signalCode == null) {
      const exited = new Promise((resolve) => supervisor.once("exit", resolve));
      supervisor.kill("SIGTERM");
      await exited;
    }
    // A test leaves nothing behind in the temp folder.
    await rm(data, { recursive: true, force: true });
  });
  const statusFile = path.join(stateDir, "status.json");
  const readStatus = async () => JSON.parse(await readFile(statusFile, "utf8"));
  const boot = await waitFor(async () => { const s = await readStatus(); return s.control_port && s; }, "no control port");
  const request = async (route, body, authenticated = true, retryBusy = true) => {
    const response = await fetch(`http://127.0.0.1:${boot.control_port}${route}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { ...(authenticated ? { authorization: "Bearer test-control-token" } : {}), "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json();
    if (retryBusy && (result.control_busy || (response.status === 409 && result.error === "control_busy"))) {
      await new Promise((resolve) => setTimeout(resolve, 40));
      return request(route, body, authenticated);
    }
    return [response.status, result];
  };
  const calls = async () => { try { return (await readFile(callsFile, "utf8")).trim().split("\n").map(JSON.parse); } catch { return []; } };
  const clean = async () => {
    const output = log + await readFile(statusFile, "utf8");
    for (const secret of secrets) assert.equal(output.includes(secret), false, "secret escaped to status or log");
  };
  return { data, stateDir, connectionFile, credentials, supervisor, readStatus, request, calls, clean, log: () => log };
}

for (const fake of [false, true]) test(`${fake ? "fake" : "real"} lifecycle, auth, restart and disconnect`, async (context) => {
  const h = await harness(context, { fake });
  assert.equal((await h.request("/status", undefined, false))[0], 401);
  assert.equal((await h.request("/livez", undefined, false))[0], 200);
  assert.equal((await h.request("/connect", {}))[0], 400);
  let [code, state] = await h.request("/connect", form);
  assert.equal(code, 200);
  assert.equal(state.real_sync_supported, true);
  assert.equal(state.simulated, fake);
  assert.equal(state.device_name, "Kitchen server");
  assert.equal(state.liveness, "child_process_only");
  assert.equal(state.last_sync_at, null);
  assert.equal(state.syncing, true);
  const persisted = JSON.parse(await readFile(h.connectionFile, "utf8"));
  assert.deepEqual(Object.keys(persisted).sort(), ["device_name", "paused", "vault_id", "vault_name"]);
  if (!fake) {
    const calls = await waitFor(async () => { const c = await h.calls(); return c.some((a) => a[0] === "sync") && c; }, "sync not invoked");
    assert.deepEqual(calls.slice(0, 4).map((a) => a[0]), ["login", "sync-create-remote", "sync-list-remote", "sync-setup"]);
    assert.deepEqual(calls[0], ["login", "--email", form.email, "--password", form.password, "--mfa", form.mfa_code]);
    assert.deepEqual(calls[1], ["sync-create-remote", "--name", form.vault_name, "--encryption", "end-to-end", "--password", form.encryption_password]);
    assert.deepEqual(calls[3], ["sync-setup", "--vault", "remote-1", "--path", path.join(h.data, "notes"), "--password", form.encryption_password, "--device-name", "Kitchen server", "--json"]);
    assert.ok(calls.some((a) => a.join(" ") === `sync --path ${path.join(h.data, "notes")} --continuous`));
    assert.equal(state.client_status.syncMode, "bidirectional");
  }
  await h.clean();
  assert.equal((await h.request("/connect", form))[0], 409);
  [code, state] = await h.request("/pause", {});
  assert.equal(code, 200);
  assert.equal(state.state, "paused");
  assert.equal(state.syncing, false);
  [code, state] = await h.request("/resume", {});
  assert.equal(code, 200);
  const oldPid = state.sync_pid;
  process.kill(oldPid, "SIGKILL");
  await waitFor(async () => { const s = await h.readStatus(); return s.syncing && s.sync_pid !== oldPid; }, "no restart");
  [code, state] = await h.request("/disconnect", {});
  assert.equal(code, 200);
  assert.equal(state.state, "not_connected");
  await assert.rejects(readFile(h.connectionFile), { code: "ENOENT" });
  if (!fake) assert.deepEqual((await h.calls()).slice(-2).map((a) => a[0]), ["sync-unlink", "logout"]);
  await h.clean();
});

test("real boot resumes without credentials; paused boot stays paused", async (context) => {
  for (const persisted of [false, true, "paused"]) {
    const h = await harness(context, { persisted });
    if (persisted === true) {
      const calls = await waitFor(async () => { const c = await h.calls(); return c.some((a) => a[0] === "sync") && c; }, "boot did not sync");
      assert.ok(calls.every((a) => ["sync", "sync-status", "sync-config"].includes(a[0])));
    } else assert.equal((await h.readStatus()).paused, persisted === "paused");
    const exited = new Promise((resolve) => h.supervisor.once("exit", resolve));
    h.supervisor.kill("SIGTERM");
    await exited;
    const stopped = await h.readStatus();
    assert.equal(stopped.state, "stopped");
    assert.equal(stopped.sync_pid, null);
    assert.equal(stopped.control_port, null);
    assert.deepEqual((await readdir(h.stateDir)).filter((n) => n.endsWith(".tmp")), []);
  }
});

test("join skips creation, and each setup failure keeps secrets out of status and log", async (context) => {
  const joined = await harness(context);
  assert.equal((await joined.request("/connect", { ...form, existing_vault: true }))[0], 200);
  assert.ok(!(await joined.calls()).some((a) => a[0] === "sync-create-remote"));
  for (const failure of ["login", "sync-create-remote", "sync-list-remote", "sync-setup"]) {
    const h = await harness(context, { failure });
    const [code, result] = await h.request("/connect", form);
    assert.equal(code, 502);
    for (const secret of secrets) assert.ok(!JSON.stringify(result).includes(secret));
    await h.clean();
    await assert.rejects(readFile(h.connectionFile), { code: "ENOENT" });
  }
});

test("log level suppresses info", async (context) => {
  const quiet = await harness(context, { level: "WARNING" });
  assert.equal(quiet.log(), "");
  const normal = await harness(context);
  await waitFor(() => normal.log().includes("control endpoint started"), "missing startup log");
  assert.equal(JSON.parse(normal.log().trim().split("\n")[0]).service, "obsidian-sync");
});

test("device settings default, scalar quoting and unknown sections", async (context) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "sync-settings-"));
  context.after(() => rm(dir, { recursive: true, force: true }));
  const file = path.join(dir, "settings.yaml");
  assert.equal(await deviceName(file), DEFAULT_DEVICE_NAME);
  for (const [text, expected] of [["other:\n  ignored: true\n", DEFAULT_DEVICE_NAME],
    ["sync:\n  device_name: 'Captain''s server'\n", "Captain's server"],
    ['sync:\n  device_name: "Kitchen server"\n', "Kitchen server"]]) {
    await writeFile(file, text);
    assert.equal(await deviceName(file), expected);
  }
});


test("absent and ambiguous remote names never reach setup", async (context) => {
  for (const remoteNames of [[], [form.vault_name, form.vault_name]]) {
    const h = await harness(context, { remoteNames });
    const [code, result] = await h.request("/connect", form);
    assert.equal(code, 502);
    assert.match(result.detail, /absent or ambiguous/);
    assert.ok(!(await h.calls()).some((args) => args[0] === "sync-setup"));
    await h.clean();
  }
});


test("saved settings apply on boot and changes preserve pause, including empty selections", async (context) => {
  const h = await harness(context, { persisted: "paused" });
  await waitFor(async () => (await h.readStatus()).config_matches, "boot did not configure");
  let state = (await h.request("/status"))[1];
  assert.ok(state.client_status.fileTypes.includes("unsupported"));
  assert.deepEqual(state.client_status.excludedFolders, ["_Trash"]);
  assert.equal(state.paused, true);
  await writeFile(path.join(h.data, "state", "settings.yaml"), JSON.stringify({sync:{
    device_name:"New name",mode:"pull-only",conflict_strategy:"conflict",file_types:["pdf"],sync_configs:[],excluded_folders:[]
  }}));
  const [code, applied] = await h.request("/configure", {});
  assert.equal(code, 200);
  assert.equal(applied.paused, true);
  assert.equal(applied.syncing, false);
  assert.equal(applied.client_status.deviceName, "New name");
  assert.equal(applied.client_status.syncMode, "pull-only");
  assert.deepEqual(applied.client_status.fileTypes, ["pdf"]);
  assert.deepEqual(applied.client_status.excludedFolders, []);
  assert.ok(!(await h.calls()).some((a) => a[0] === "sync"));
});

test("failed apply does not start sync or claim success", async (context) => {
  const h = await harness(context, {persisted:true, failure:"sync-config"});
  await waitFor(async () => (await h.readStatus()).config_error, "missing config failure");
  const [code, state] = await h.request("/configure", {});
  assert.equal(code, 502);
  assert.equal(state.config_matches, false);
  assert.equal(state.syncing, false);
  assert.ok(!(await h.calls()).some((a) => a[0] === "sync"));
  await h.clean();
});

test("failed live read never returns cached configuration as live", async (context) => {
  const h = await harness(context, {persisted:true, failure:"sync-status"});
  await waitFor(async () => (await h.readStatus()).config_error, "missing status failure");
  const state = (await h.request("/status"))[1];
  assert.equal(state.config_live, false);
  assert.equal(state.client_status, null);
  assert.equal(state.config_matches, false);
});


for (const failure of ["sync-status", "sync-config"]) test(`initial ${failure} failure is not reported as connected successfully`, async (context) => {
  const h = await harness(context, { failure });
  const [code, state] = await h.request("/connect", form);
  assert.equal(code, 502);
  assert.equal(state.configured, true);
  assert.equal(state.config_matches, false);
  assert.equal(state.syncing, false);
  assert.ok(state.config_error);
  assert.ok(!(await h.calls()).some(args => args[0] === "sync"));
  await h.clean();
});

test("status stays available during a blocked configuration command", async (context) => {
  const h = await harness(context);
  assert.equal((await h.request("/connect", form))[0], 200);
  const block = path.join(h.data, "block-status");
  await writeFile(block, "yes");
  const configuring = h.request("/configure", {});
  try {
    await waitFor(() => readFile(path.join(h.data, "status-blocked")), "client did not block");
    const [code, state] = await h.request("/status", undefined, true, false);
    assert.equal(code, 200);
    assert.equal(state.configured, true);
    assert.equal(state.vault_name, form.vault_name);
    assert.equal(state.config_live, false);
    assert.equal(state.config_matches, false);
    assert.equal(state.control_busy, true);
    assert.equal((await h.request("/pause", {}, true, false))[0], 409);
  } finally {
    await rm(block, { force: true });
    await configuring;
  }
  const [code, state] = await h.request("/status");
  assert.equal(code, 200);
  assert.equal(state.config_live, true);
  assert.equal(state.config_matches, true);
});

test("an account is signed in step by step, and signing in again needs only the password", async (context) => {
  const h = await harness(context, { fake: true });
  const account = path.join(h.credentials, "coppermind-account");
  // A wrong password is a wrong password. Only a request for a code says a code is needed.
  let [code, body] = await h.request("/login", { email: form.email, password: "wrong" });
  assert.deepEqual([code, body.error, body.mfa_needed], [502, "login_failed", false]);
  [code, body] = await h.request("/login", { email: form.email, password: "mfa" });
  assert.deepEqual([code, body.mfa_needed], [502, true]);
  [code, body] = await h.request("/vault", { vault_id: "fake-1" });
  assert.deepEqual([code, body.error], [409, "not_signed_in"]);
  [code, body] = await h.request("/login", { email: form.email, password: "mfa", mfa_code: "123456" });
  assert.equal(code, 200);
  assert.equal(body.signed_in, true);
  // The vaults on the account, with whether each has a password of its own and never the password.
  assert.deepEqual(body.vaults.map((v) => [v.id, v.name, v.locked]), [["fake-1", "My notes", true], ["fake-2", "Work archive", false]]);
  assert.equal(JSON.stringify(body).includes("vault-password"), false);
  // A locked vault needs its password, and the right one.
  assert.deepEqual((await h.request("/vault", { vault_id: "fake-1" }))[1].error, "password_required");
  assert.deepEqual((await h.request("/vault", { vault_id: "fake-1", password: "nope" }))[1].error, "wrong_password");
  assert.equal((await h.request("/connect", {}))[0], 400);
  [code, body] = await h.request("/vault", { vault_id: "fake-1", password: "vault-password" });
  assert.equal(code, 200);
  assert.deepEqual(body.choice, { vault_id: "fake-1", vault_name: "My notes", new_vault: false });
  // Nothing is remembered until a vault is actually connected.
  await assert.rejects(readFile(account), { code: "ENOENT" });
  [code, body] = await h.request("/connect", {});
  assert.equal(code, 200);
  assert.equal(body.vault_name, "My notes");
  assert.equal(body.account_known, true);
  // The address is on the credential volume and nowhere a backup or a log would carry it.
  assert.equal((await readFile(account, "utf8")).trim(), form.email);
  for (const name of await readdir(h.stateDir)) {
    assert.equal((await readFile(path.join(h.stateDir, name), "utf8")).includes(form.email), false, name);
  }
  assert.equal(h.log().includes(form.email), false);

  // Obsidian signs this server out: the client keeps stopping, and the helper says why.
  await writeFile(path.join(h.stateDir, "fake-signed-out"), "");
  const stopped = await waitFor(async () => { const s = await h.readStatus(); return s.needs_signin && s; }, "sign-out not noticed", 2500);
  assert.equal(stopped.syncing, false);
  assert.equal(stopped.account_known, true);
  // Only the password is asked for, and a wrong one changes nothing.
  [code, body] = await h.request("/signin", { password: "wrong" });
  assert.deepEqual([code, body.error, body.mfa_needed], [502, "login_failed", false]);
  assert.equal((await h.readStatus()).needs_signin, true);
  [code, body] = await h.request("/signin", { password: "the new password" });
  assert.equal(code, 200);
  assert.equal(body.needs_signin, false);
  await waitFor(async () => (await h.readStatus()).syncing, "sync did not restart");
  // Disconnecting forgets the account as well.
  assert.equal((await h.request("/disconnect", {}))[0], 200);
  await assert.rejects(readFile(account), { code: "ENOENT" });
  assert.equal((await h.readStatus()).account_known, false);
});

test("a server that connected before the account was kept is asked for it once", async (context) => {
  const h = await harness(context, { fake: true, persisted: true });
  assert.equal((await h.readStatus()).account_known, false);
  let [code, body] = await h.request("/signin", { password: "anything" });
  assert.deepEqual([code, body.error], [400, "account_required"]);
  [code, body] = await h.request("/signin", { email: form.email, password: "anything" });
  assert.equal(code, 200);
  assert.equal(body.account_known, true);
  assert.equal((await h.request("/signin", { password: "anything" }))[0], 200);
});

test("real mode: a signed-out client is left alone by resume and configure until the password is given", async (context) => {
  const h = await harness(context, { persisted: true });
  const syncs = async () => (await h.calls()).filter((a) => a[0] === "sync").length;
  await waitFor(async () => (await syncs()) > 0, "boot did not sync");
  await writeFile(path.join(h.data, "signed-out"), "");
  await writeFile(path.join(h.data, "sync-exits"), "");
  assert.equal((await h.request("/pause", {}))[0], 200);
  await h.request("/resume", {});
  const stopped = await waitFor(async () => { const s = await h.readStatus(); return s.needs_signin && s; }, "sign-out not noticed", 4000);
  assert.equal(stopped.syncing, false);
  const before = await syncs();
  await h.request("/resume", {});
  await h.request("/configure", {});
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.equal(await syncs(), before, "a signed-out client was started again");
  assert.equal((await h.readStatus()).needs_signin, true);
  await rm(path.join(h.data, "sync-exits"));
  const [code, body] = await h.request("/signin", { email: form.email, password: form.password });
  assert.equal(code, 200);
  assert.equal(body.needs_signin, false);
  await waitFor(async () => (await syncs()) > before, "sync did not restart after signing in");
});

test("real mode: changing sync settings while signed out shows the signed-out state, not 'starting'", async (context) => {
  const h = await harness(context, { persisted: true });
  const syncs = async () => (await h.calls()).filter((a) => a[0] === "sync").length;
  await waitFor(async () => (await syncs()) > 0, "boot did not sync");
  await writeFile(path.join(h.data, "signed-out"), "");
  await writeFile(path.join(h.data, "sync-exits"), "");
  await h.request("/pause", {});
  await h.request("/resume", {});
  await waitFor(async () => { const s = await h.readStatus(); return s.needs_signin && s; }, "sign-out not noticed", 4000);
  await writeFile(path.join(h.data, "state", "settings.yaml"), "sync:\n  device_name: 'Renamed server'\n");
  await h.request("/configure", {});
  const after = await h.readStatus();
  assert.equal(after.needs_signin, true);
  assert.equal(after.state, "error");
  assert.equal(after.syncing, false);
});

test("real mode: a client started while the signed-out check runs is not reported as signed out", async (context) => {
  const h = await harness(context, { persisted: true });
  const syncs = async () => (await h.calls()).filter((a) => a[0] === "sync").length;
  const probes = async () => (await h.calls()).filter((a) => a.length === 1 && a[0] === "login").length;
  await waitFor(async () => (await syncs()) > 0, "boot did not sync");
  await writeFile(path.join(h.data, "signed-out"), "");
  await writeFile(path.join(h.data, "sync-exits"), "");
  await writeFile(path.join(h.data, "login-hold"), "");
  await h.request("/pause", {});
  await h.request("/resume", {});
  await waitFor(async () => (await probes()) > 0, "the signed-out check never started", 200);
  await rm(path.join(h.data, "sync-exits"));
  const before = await syncs();
  await h.request("/resume", {});
  await waitFor(async () => (await syncs()) > before, "a client was not started during the check");
  await rm(path.join(h.data, "login-hold"));
  await new Promise((resolve) => setTimeout(resolve, 1500));
  const status = await h.readStatus();
  assert.equal(status.needs_signin && status.syncing, false, "signed out and syncing at once");
});

test("real mode: a locked vault asks for its own password, a wrong one is refused, and the right one is used to connect", async (context) => {
  const h = await harness(context, { remoteNames: ["Locked vault"] });
  await writeFile(path.join(h.data, "vault-locked"), "");
  let [code, body] = await h.request("/login", { email: form.email, password: form.password });
  assert.equal(code, 200);
  assert.deepEqual(body.vaults.map((v) => [v.id, v.name, v.locked]), [["remote-1", "Locked vault", true]]);
  assert.equal(JSON.stringify(body).includes("vault-secret"), false);
  assert.equal((await h.request("/vault", { vault_id: "remote-1" }))[1].error, "password_required");
  assert.equal((await h.request("/vault", { vault_id: "remote-1", password: "nope" }))[1].error, "wrong_password");
  [code, body] = await h.request("/vault", { vault_id: "remote-1", password: "vault-secret" });
  assert.equal(code, 200);
  assert.deepEqual(body.choice, { vault_id: "remote-1", vault_name: "Locked vault", new_vault: false });
  [code, body] = await h.request("/connect", {});
  assert.equal(code, 200);
  assert.equal(body.vault_name, "Locked vault");
  const setup = (await h.calls()).filter((a) => a[0] === "sync-setup" && a.includes(path.join(h.data, "notes")));
  assert.equal(setup.length, 1);
  assert.equal(setup[0][setup[0].indexOf("--password") + 1], "vault-secret");
  assert.equal((await readFile(h.connectionFile, "utf8")).includes("vault-secret"), false);
  assert.equal(h.log().includes("vault-secret"), false);
});

test("real mode: a vault that is not locked connects without asking for a password", async (context) => {
  const h = await harness(context, { remoteNames: ["Open vault"] });
  let [code, body] = await h.request("/login", { email: form.email, password: form.password });
  assert.deepEqual(body.vaults.map((v) => v.locked), [false]);
  [code, body] = await h.request("/vault", { vault_id: "remote-1" });
  assert.equal(code, 200);
  [code, body] = await h.request("/connect", {});
  assert.equal(code, 200);
  const setup = (await h.calls()).filter((a) => a[0] === "sync-setup" && a.includes(path.join(h.data, "notes")));
  assert.equal(setup[0].includes("--password"), false);
});

test("real mode: signing in again with the password alone restarts a signed-out client", async (context) => {
  const h = await harness(context, { persisted: true });
  const syncs = async () => (await h.calls()).filter((a) => a[0] === "sync").length;
  await waitFor(async () => (await syncs()) > 0, "boot did not sync");
  await writeFile(path.join(h.data, "signed-out"), "");
  await writeFile(path.join(h.data, "sync-exits"), "");
  await h.request("/pause", {});
  await h.request("/resume", {});
  await waitFor(async () => { const s = await h.readStatus(); return s.needs_signin && s; }, "sign-out not noticed", 4000);
  await rm(path.join(h.data, "sync-exits"));
  const [code, body] = await h.request("/signin", { email: form.email, password: form.password });
  assert.equal(code, 200);
  assert.equal(body.needs_signin, false);
  const login = (await h.calls()).filter((a) => a[0] === "login" && a.includes("--password")).pop();
  assert.deepEqual(login, ["login", "--email", form.email, "--password", form.password]);
  await h.clean();
});

test("real mode: pausing and resuming while signed out shows the signed-out state, not 'paused'", async (context) => {
  const h = await harness(context, { persisted: true });
  const syncs = async () => (await h.calls()).filter((a) => a[0] === "sync").length;
  await waitFor(async () => (await syncs()) > 0, "boot did not sync");
  await writeFile(path.join(h.data, "signed-out"), "");
  await writeFile(path.join(h.data, "sync-exits"), "");
  await h.request("/pause", {});
  await h.request("/resume", {});
  await waitFor(async () => { const s = await h.readStatus(); return s.needs_signin && s; }, "sign-out not noticed", 4000);
  assert.equal((await h.request("/pause", {}))[1].state, "paused");
  const [, resumed] = await h.request("/resume", {});
  assert.equal(resumed.paused, false);
  assert.equal(resumed.needs_signin, true);
  assert.equal(resumed.state, "error");
  assert.equal((await h.readStatus()).state, "error");
});
