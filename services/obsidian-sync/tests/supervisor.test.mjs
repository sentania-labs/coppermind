import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const ROOT = path.resolve(import.meta.dirname, "..");

async function waitFor(check, message, attempts = 100) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const result = await check();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${message}${lastError ? `: ${lastError.message}` : ""}`);
}

test("fake mode proves the authenticated lifecycle and child restart", async (context) => {
  const data = await mkdtemp(path.join(os.tmpdir(), "coppermind-sync-"));
  const tokenFile = path.join(data, "internal-token");
  await mkdir(path.dirname(tokenFile), { recursive: true });
  await writeFile(tokenFile, "test-control-token\n", { mode: 0o600 });
  const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
    env: {
      ...process.env,
      COPPERMIND_DATA_DIR: data,
      COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile,
      COPPERMIND_SYNC_FAKE: "1",
      COPPERMIND_SYNC_PORT: "0",
    },
    stdio: "ignore",
  });
  context.after(() => supervisor.kill("SIGTERM"));

  const statusFile = path.join(data, "state", "sync", "status.json");
  const initial = await waitFor(async () => {
    const value = JSON.parse(await readFile(statusFile, "utf8"));
    return value.control_port ? value : null;
  }, "supervisor did not publish its port");
  const base = `http://127.0.0.1:${initial.control_port}`;
  const request = async (route, method = "GET", authenticated = true, body = undefined) => {
    const response = await fetch(`${base}${route}`, {
      method,
      headers: {
        ...(authenticated ? { authorization: "Bearer test-control-token" } : {}),
        ...(body ? { "content-type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    return [response.status, await response.json()];
  };

  assert.deepEqual(await request("/livez", "GET", false), [200, { supervisor: "running" }]);
  assert.deepEqual(await request("/healthz", "GET", false), [
    503,
    { healthy: false, state: "not_connected" },
  ]);
  assert.equal((await request("/status", "GET", false))[0], 401);
  let [code, state] = await request("/status");
  assert.equal(code, 200);
  assert.equal(state.connected, false);
  assert.equal(state.syncing, false);
  assert.equal(state.state, "not_connected");

  [code, state] = await request("/connect", "POST", true, {});
  assert.equal(code, 200);
  assert.equal(state.connected, true);
  assert.equal(state.syncing, true);
  assert.equal((await request("/healthz", "GET", false))[0], 200);
  const firstPid = state.sync_pid;

  [code, state] = await request("/pause", "POST");
  assert.equal(code, 200);
  assert.equal(state.state, "paused");
  assert.equal(state.connected, false);
  assert.equal(state.syncing, false);
  assert.equal((await request("/healthz", "GET", false))[0], 503);

  [code, state] = await request("/resume", "POST");
  assert.equal(code, 200);
  assert.equal(state.connected, true);
  assert.equal(state.syncing, true);
  assert.equal((await request("/healthz", "GET", false))[0], 200);
  assert.notEqual(state.sync_pid, firstPid);
  const resumedPid = state.sync_pid;

  process.kill(resumedPid, "SIGKILL");
  state = await waitFor(async () => {
    const [, value] = await request("/status");
    return value.syncing && value.sync_pid !== resumedPid ? value : null;
  }, "supervisor did not restart the killed sync process");
  assert.equal(state.connected, true);
  assert.equal(state.last_error, null);

  const persisted = JSON.parse(await readFile(statusFile, "utf8"));
  assert.equal(persisted.sync_pid, state.sync_pid);
  assert.equal(persisted.state, "syncing");
});
