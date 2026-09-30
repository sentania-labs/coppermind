import { readFile } from "node:fs/promises";

export const DEFAULT_DEVICE_NAME = "coppermind-server";

// Read only our scalar from the block mapping emitted by StateStore. Unknown
// sections do not couple this helper to the Python settings schema.
export async function deviceName(file) {
  let text;
  try { text = await readFile(file, "utf8"); }
  catch (error) { if (error.code === "ENOENT") return DEFAULT_DEVICE_NAME; throw error; }
  let value;
  if (text.trimStart().startsWith("{")) value = JSON.parse(text).sync?.device_name;
  else {
    let inSync = false;
    for (const line of text.split(/\r?\n/)) {
      if (/^\s*(#.*)?$/.test(line)) continue;
      if (/^\S/.test(line)) {
        inSync = /^sync:\s*(?:#.*)?$/.test(line);
        if (/^sync:/.test(line) && !inSync) throw new Error("sync settings must be a block mapping");
      } else if (inSync && /^  device_name:/.test(line)) {
        const raw = line.slice(line.indexOf(":") + 1).trim();
        if (raw.startsWith('"')) value = JSON.parse(raw.replace(/\s+#.*$/, ""));
        else if (raw.startsWith("'")) {
          const match = raw.match(/^'((?:[^']|'')*)'\s*(?:#.*)?$/);
          if (!match) throw new Error("invalid device name setting");
          value = match[1].replaceAll("''", "'");
        } else {
          value = raw.replace(/\s+#.*$/, "");
          if (/^[>|&*!{\[]/.test(value)) throw new Error("unsupported device name scalar");
          if (["", "null", "~"].includes(value)) value = undefined;
        }
      }
    }
  }
  if (value == null) return DEFAULT_DEVICE_NAME;
  if (typeof value !== "string" || !value.trim() || /[\r\n\0]/.test(value)) {
    throw new Error("invalid device name setting");
  }
  return value;
}
