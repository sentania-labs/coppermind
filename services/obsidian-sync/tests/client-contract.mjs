// Run in the built image with no network. Synthetic vault metadata exercises
// only configuration commands, never account login or file synchronization.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { syncSettings, clientMatches, FILE_TYPES, CONFIG_TYPES } from "../settings.mjs";

test("pinned Obsidian client round-trips all supported settings", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "sync-contract-"));
  try {
    const vaultPath = path.join(root, "notes");
    const config = path.join(root, "obsidian-headless", "sync", "test");
    await mkdir(config, { recursive: true });
    await writeFile(path.join(config, "config.json"), JSON.stringify({
      vaultId: "test", vaultName: "test", vaultPath, deviceName: "test",
      conflictStrategy: "merge", encryptionKey: "test", encryptionSalt: "test", encryptionVersion: 3,
    }));
    function command(args) {
      const result = spawnSync("ob", [...args, "--path", vaultPath, "--json"], {
        env: { ...process.env, XDG_CONFIG_HOME: root }, encoding: "utf8",
      });
      assert.equal(result.status, 0, result.stderr);
      return JSON.parse(result.stdout);
    }
    const selections = [...FILE_TYPES.map((type) => [type]), FILE_TYPES];
    for (const [index, types] of selections.entries()) {
      const desired = {
        device_name: "Contract device", mode: ["bidirectional", "pull-only", "mirror-remote"][index % 3],
        conflict_strategy: index % 2 ? "merge" : "conflict", file_types: types,
        sync_configs: index % 2 ? CONFIG_TYPES : [], excluded_folders: index % 2 ? ["_Trash", "Folder with spaces"] : [],
      };
      command(["sync-config", "--device-name", desired.device_name, "--mode", desired.mode,
        "--conflict-strategy", desired.conflict_strategy, "--file-types", types.join(","),
        "--configs", desired.sync_configs.join(","), "--excluded-folders", desired.excluded_folders.join(",")]);
      assert.ok(clientMatches(desired, command(["sync-status"])), JSON.stringify(desired));
    }
    // Empty file types restore client defaults, so reject them before stopping sync.
    assert.deepEqual(command(["sync-config", "--file-types", ""]).fileTypes.sort(), ["audio", "image", "pdf", "video"]);
    const settings = path.join(root, "settings.yaml");
    await writeFile(settings, JSON.stringify({ sync: { file_types: [] } }));
    await assert.rejects(syncSettings(settings), /At least one attachment type/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
