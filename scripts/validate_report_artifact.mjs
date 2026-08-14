import fs from "node:fs";
import { createRequire } from "node:module";

const [pluginRoot, input] = process.argv.slice(2);
if (!pluginRoot || !input) {
  throw new Error(
    "usage: validate_report_artifact.mjs PLUGIN_ROOT artifact.json",
  );
}
const require = createRequire(import.meta.url);
const server = require(`${pluginRoot}/mcp/server.cjs`);
const payload = JSON.parse(fs.readFileSync(input, "utf8"));
const result = server.callTool("validate_artifact", payload);
if (!result || result.ok !== true) {
  throw new Error("validate_artifact did not return ok=true");
}
process.stdout.write(`${JSON.stringify({ ok: true, surface: result.surface })}\n`);
