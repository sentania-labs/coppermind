import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
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
  assert.equal((await request("/status", "GET", false))[0], 401);
  let [code, state] = await request("/status");
  assert.equal(code, 200);
  assert.equal(state.connected, false);
  assert.equal(state.syncing, false);
  assert.equal(state.state, "not_connected");

  for (const body of [{}, { vault_name: "" }, { vault_name: 7 }]) {
    const [rejected, detail] = await request("/connect", "POST", true, body);
    assert.equal(rejected, 400, `connect accepted ${JSON.stringify(body)}`);
    assert.equal(detail.error, "vault_name_required");
  }
  [, state] = await request("/status");
  assert.equal(state.configured, false, "a rejected connect still configured the helper");
  assert.equal(state.vault_name, null);

  [code, state] = await request("/connect", "POST", true, { vault_name: "Simulated remote vault" });
  assert.equal(code, 200);
  assert.equal(state.vault_name, "Simulated remote vault");
  assert.equal(state.connected, true);
  assert.equal(state.syncing, true);
  assert.equal(state.simulated, true);
  assert.equal(state.real_sync_supported, false);
  assert.equal("device_name" in state, false);
  const firstPid = state.sync_pid;

  [code, state] = await request("/pause", "POST");
  assert.equal(code, 200);
  assert.equal(state.state, "paused");
  assert.equal(state.connected, false);
  assert.equal(state.syncing, false);

  [code, state] = await request("/resume", "POST");
  assert.equal(code, 200);
  assert.equal(state.connected, true);
  assert.equal(state.syncing, true);
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

  const reported = await waitFor(async () => {
    const [, value] = await request("/status");
    return value.sync_mode ? value : null;
  }, "supervisor never published what the client reported about itself");
  assert.equal(reported.sync_mode, "simulated");
  assert.equal(reported.conflict_strategy, "simulated");
  assert.equal(reported.liveness, "child_process_only");
});

test("real mode refuses every path that would reach the account or the remote vault", async (context) => {
  const data = await mkdtemp(path.join(os.tmpdir(), "coppermind-sync-real-"));
  const tokenFile = path.join(data, "internal-token");
  await writeFile(tokenFile, "test-control-token\n", { mode: 0o600 });
  // A connection persisted by a simulated run must not restart real sync on
  // boot, and its paused flag must not stand in for the refusal.
  await mkdir(path.join(data, "state", "sync"), { recursive: true });
  await writeFile(
    path.join(data, "state", "sync", "connection.json"),
    `${JSON.stringify({ vault_name: "Captain vault", paused: true })}\n`,
  );
  // Any call to the Obsidian client lands here instead of the real binary.
  const bin = path.join(data, "bin");
  const invoked = path.join(data, "ob-was-invoked");
  await mkdir(bin, { recursive: true });
  await writeFile(path.join(bin, "ob"), `#!/bin/sh\necho "$@" >> ${invoked}\n`, { mode: 0o755 });

  const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      COPPERMIND_DATA_DIR: data,
      COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile,
      COPPERMIND_SYNC_PORT: "0",
    },
    stdio: "ignore",
  });
  context.after(() => supervisor.kill("SIGTERM"));

  const statusFile = path.join(data, "state", "sync", "status.json");
  const booted = await waitFor(async () => {
    const value = JSON.parse(await readFile(statusFile, "utf8"));
    return value.control_port ? value : null;
  }, "supervisor did not publish its port");
  const base = `http://127.0.0.1:${booted.control_port}`;
  const request = async (route, method = "GET", body = undefined) => {
    const response = await fetch(`${base}${route}`, {
      method,
      headers: {
        authorization: "Bearer test-control-token",
        ...(body ? { "content-type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    return [response.status, await response.json()];
  };

  const boot = await waitFor(async () => {
    const [, value] = await request("/status");
    return value.state === "refused" ? value : null;
  }, "supervisor did not refuse the persisted connection on boot");
  assert.equal(boot.connected, false);
  assert.equal(boot.syncing, false);
  assert.equal(boot.sync_pid, null);
  assert.equal(boot.simulated, false);
  assert.equal(boot.real_sync_supported, false);
  assert.equal(boot.sync_mode, null);
  assert.equal(boot.conflict_strategy, null);
  assert.equal(boot.paused, false);
  assert.match(boot.last_error, /pending captain decisions/);
  assert.equal("device_name" in boot, false);

  for (const route of ["/connect", "/pause", "/resume"]) {
    const [code, body] = await request(route, "POST", { vault_name: "Captain vault" });
    assert.equal(code, 501, `${route} did not refuse`);
    assert.equal(body.error, "real_sync_refused");
    assert.match(body.detail, /pending captain decisions/);
  }

  const [, after] = await request("/status");
  assert.equal(after.connected, false);
  assert.equal(after.syncing, false);
  assert.equal(after.state, "refused");
  assert.match(after.last_error, /pending captain decisions/);
  await assert.rejects(readFile(invoked), { code: "ENOENT" }, "the Obsidian client was invoked");
});

test("the supervisor honours COPPERMIND_LOG_LEVEL", async (context) => {
  // Its log lines are the contract here: one JSON object per line on stdout,
  // the field names every other Coppermind service uses.
  const linesAt = async (level) => {
    const data = await mkdtemp(path.join(os.tmpdir(), "coppermind-sync-log-"));
    const tokenFile = path.join(data, "internal-token");
    await writeFile(tokenFile, "test-control-token\n", { mode: 0o600 });
    await mkdir(path.join(data, "state", "sync"), { recursive: true });
    await writeFile(
      path.join(data, "state", "sync", "connection.json"),
      `${JSON.stringify({ vault_name: "Captain vault", paused: false })}\n`,
    );
    const env = { ...process.env };
    delete env.COPPERMIND_LOG_LEVEL;
    if (level) env.COPPERMIND_LOG_LEVEL = level;
    const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
      env: {
        ...env,
        COPPERMIND_DATA_DIR: data,
        COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile,
        COPPERMIND_SYNC_PORT: "0",
      },
      stdio: ["ignore", "pipe", "ignore"],
    });
    context.after(() => supervisor.kill("SIGTERM"));
    let out = "";
    supervisor.stdout.on("data", (chunk) => {
      out += chunk;
    });
    await waitFor(
      async () => out.includes("real sync refused"),
      `the supervisor never logged the refusal at ${level ?? "the default level"}`,
    );
    return out.trim().split("\n").map(JSON.parse);
  };

  const quiet = await linesAt("WARNING");
  assert.deepEqual([...new Set(quiet.map((entry) => entry.level))], ["warning"]);
  assert.equal(
    quiet.some((entry) => entry.event === "control endpoint started"),
    false,
    "an info line survived COPPERMIND_LOG_LEVEL=WARNING",
  );

  const chatty = await linesAt(undefined);
  const startup = chatty.find((entry) => entry.event === "control endpoint started");
  assert.ok(startup, "the default level dropped the startup line");
  assert.equal(startup.level, "info");
  assert.equal(startup.service, "obsidian-sync");
  assert.ok(
    chatty.some((entry) => entry.event === "real sync refused" && entry.level === "warning"),
    "the refusal is not logged as a warning",
  );
});

test("a stopped supervisor leaves the stop in the status file", async (context) => {
  const data = await mkdtemp(path.join(os.tmpdir(), "coppermind-sync-stop-"));
  const tokenFile = path.join(data, "internal-token");
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
  context.after(() => supervisor.kill("SIGKILL"));

  const stateDir = path.join(data, "state", "sync");
  const statusFile = path.join(stateDir, "status.json");
  const started = await waitFor(async () => {
    const value = JSON.parse(await readFile(statusFile, "utf8"));
    return value.control_port ? value : null;
  }, "supervisor did not publish its port");
  const connected = await fetch(`http://127.0.0.1:${started.control_port}/connect`, {
    method: "POST",
    headers: { authorization: "Bearer test-control-token", "content-type": "application/json" },
    body: JSON.stringify({ vault_name: "Simulated remote vault" }),
  });
  assert.equal(connected.status, 200);
  assert.equal((await connected.json()).syncing, true);

  supervisor.kill("SIGTERM");
  await new Promise((resolve) => supervisor.once("exit", resolve));

  const persisted = JSON.parse(await readFile(statusFile, "utf8"));
  assert.equal(persisted.state, "stopped");
  assert.equal(persisted.syncing, false);
  assert.equal(persisted.connected, false);
  assert.equal(persisted.sync_pid, null);
  assert.equal(persisted.control_port, null);
  assert.deepEqual(
    (await readdir(stateDir)).filter((name) => name.endsWith(".tmp")),
    [],
    "a half-written status file was left on the volume",
  );
});

test("shutdown persists stopped from every state without a child", async (context) => {
  const cases = [
    { name: "disconnected", fake: false, connection: null, expectedState: "not_connected" },
    {
      name: "refused",
      fake: false,
      connection: { vault_name: "Captain vault", paused: false },
      expectedState: "refused",
    },
    {
      name: "paused",
      fake: true,
      connection: { vault_name: "Simulated remote vault", paused: true },
      expectedState: "paused",
    },
  ];

  for (const scenario of cases) {
    await context.test(scenario.name, async () => {
      const data = await mkdtemp(path.join(os.tmpdir(), `coppermind-sync-${scenario.name}-`));
      const tokenFile = path.join(data, "internal-token");
      await writeFile(tokenFile, "test-control-token\n", { mode: 0o600 });
      if (scenario.connection) {
        const stateDir = path.join(data, "state", "sync");
        await mkdir(stateDir, { recursive: true });
        await writeFile(
          path.join(stateDir, "connection.json"),
          `${JSON.stringify(scenario.connection)}\n`,
        );
      }

      const supervisor = spawn(process.execPath, [path.join(ROOT, "supervisor.mjs")], {
        env: {
          ...process.env,
          COPPERMIND_DATA_DIR: data,
          COPPERMIND_INTERNAL_TOKEN_FILE: tokenFile,
          COPPERMIND_SYNC_FAKE: scenario.fake ? "1" : "0",
          COPPERMIND_SYNC_PORT: "0",
        },
        stdio: "ignore",
      });
      context.after(() => supervisor.kill("SIGKILL"));

      const statusFile = path.join(data, "state", "sync", "status.json");
      await waitFor(async () => {
        const value = JSON.parse(await readFile(statusFile, "utf8"));
        return value.control_port && value.state === scenario.expectedState ? value : null;
      }, `supervisor did not reach ${scenario.expectedState}`);

      supervisor.kill("SIGTERM");
      await new Promise((resolve) => supervisor.once("exit", resolve));

      const persisted = JSON.parse(await readFile(statusFile, "utf8"));
      assert.equal(persisted.state, "stopped");
      assert.equal(persisted.connected, false);
      assert.equal(persisted.syncing, false);
      assert.equal(persisted.sync_pid, null);
      assert.equal(persisted.control_port, null);
    });
  }
});
