import { readFile } from "node:fs/promises";
import { parse } from "yaml";

export const DEFAULT_DEVICE_NAME = "coppermind-server";
export const FILE_TYPES = ["image", "audio", "video", "pdf", "unsupported"];
export const CONFIG_TYPES = ["app", "appearance", "appearance-data", "hotkey", "core-plugin",
  "core-plugin-data", "community-plugin", "community-plugin-data"];

export async function syncSettings(file) {
  let document;
  try { document = parse(await readFile(file, "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const s = document?.sync ?? {};
  const result = {
    device_name: s.device_name ?? DEFAULT_DEVICE_NAME,
    mode: s.mode ?? "bidirectional", conflict_strategy: s.conflict_strategy ?? "merge",
    file_types: s.file_types ?? [...FILE_TYPES], sync_configs: s.sync_configs ?? [],
    excluded_folders: s.excluded_folders ?? ["_Trash"],
  };
  if (typeof result.device_name !== "string" || !result.device_name.trim() || /[\r\n\0]/.test(result.device_name)
      || !["bidirectional", "pull-only", "mirror-remote"].includes(result.mode)
      || !["merge", "conflict"].includes(result.conflict_strategy)) throw new Error("Invalid sync settings");
  for (const [key, allowed] of [["file_types", FILE_TYPES], ["sync_configs", CONFIG_TYPES]]) {
    if (!Array.isArray(result[key]) || result[key].some((v) => !allowed.includes(v))) throw new Error("Invalid sync selection");
    result[key] = [...new Set(result[key])];
  }
  if (!result.file_types.length) throw new Error("At least one attachment type is required by this client");
  if (!Array.isArray(result.excluded_folders) || result.excluded_folders.some((v) =>
    typeof v !== "string" || /[,\\\r\n\0]/.test(v) || v.split("/").some((p) => ["", ".", ".."].includes(p)))) {
    throw new Error("Invalid excluded folders");
  }
  result.excluded_folders = [...new Set(result.excluded_folders)];
  return result;
}

export async function deviceName(file) {
  return (await syncSettings(file)).device_name;
}

export function clientMatches(settings, report) {
  if (!report) return false;
  return [["device_name", "deviceName"], ["mode", "syncMode"], ["conflict_strategy", "conflictStrategy"],
    ["file_types", "fileTypes"], ["sync_configs", "configs"], ["excluded_folders", "excludedFolders"]]
    .every(([key, remote]) => Array.isArray(settings[key])
      ? Array.isArray(report[remote]) && JSON.stringify([...settings[key]].sort()) === JSON.stringify([...report[remote]].sort())
      : settings[key] === report[remote]);
}
