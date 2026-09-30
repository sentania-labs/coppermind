import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { deviceName, DEFAULT_DEVICE_NAME } from "../settings.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const form = { email: "operator@example.invalid", password: "account-test-secret", mfa_code: "482916",
  encryption_password: "encryption-test-secret", vault_name: "Phone notes", existing_vault: false };
const secrets = [form.email, form.password, form.mfa_code, form.encryption_password];

async function waitFor(check, message) {
  for (let attempt = 0; attempt < 150; attempt += 1) {
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
  const callsFile = path.join(data, "calls.jsonl");
  // Test-only argv capture intentionally contains synthetic secrets. Production
  // never records argv. Split stderr checks redaction across stream chunks.
  await writeFile(path.join(bin, "ob"), `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(${JSON.stringify(callsFile)}, JSON.stringify(args) + '\\n');
if (args[0] === ${JSON.stringify(failure)}) {
  process.stderr.write(${JSON.stringify(secrets.join(" ").slice(0, 10))});
  setTimeout(() => { process.stderr.write(${JSON.stringify(secrets.join(" ").slice(10))}); process.exit(2); }, 10);
} else if (args[0] === 'sync-list-remote') console.log(JSON.stringify({vaults:${JSON.stringify(remoteNames.map((name) => ({ id: 'remote-1', name })))},shared:[]}));
else if (args[0] === 'sync-status' || args[0] === 'sync-setup') console.log(JSON.stringify({vaultId:'remote-1',vaultName:'Phone notes',syncMode:'bidirectional',conflictStrategy:'merge',deviceName:'Kitchen server'}));
else if (args[0] === 'login') console.log(${JSON.stringify(secrets.join(' '))});
else if (args[0] === 'sync') { console.log(${JSON.stringify(secrets.join(' '))}); console.error(${JSON.stringify(secrets.join(' '))}); setInterval(() => {}, 1000); }
`, { mode: 0o755 });
  let log = "";
  const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
    env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, COPPERMIND_DATA_DIR: data,
      COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile, COPPERMIND_SYNC_FAKE: fake ? "1" : "0",
      COPPERMIND_SYNC_PORT: "0", COPPERMIND_LOG_LEVEL: level },
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
  });
  const statusFile = path.join(stateDir, "status.json");
  const readStatus = async () => JSON.parse(await readFile(statusFile, "utf8"));
  const boot = await waitFor(async () => { const s = await readStatus(); return s.control_port && s; }, "no control port");
  const request = async (route, body, authenticated = true) => {
    const response = await fetch(`http://127.0.0.1:${boot.control_port}${route}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { ...(authenticated ? { authorization: "Bearer test-control-token" } : {}), "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return [response.status, await response.json()];
  };
  const calls = async () => { try { return (await readFile(callsFile, "utf8")).trim().split("\n").map(JSON.parse); } catch { return []; } };
  const clean = async () => {
    const output = log + await readFile(statusFile, "utf8");
    for (const secret of secrets) assert.equal(output.includes(secret), false, "secret escaped to status or log");
  };
  return { data, stateDir, connectionFile, supervisor, readStatus, request, calls, clean, log: () => log };
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
      assert.ok(calls.every((a) => ["sync", "sync-status"].includes(a[0])));
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

test("device settings default, scalar quoting and unknown sections", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "sync-settings-"));
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
