import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";

// Exercise Playwright's real configuration load without needing a browser/server.
function list(env) {
  const result = spawnSync(process.execPath, [
    "node_modules/@playwright/test/cli.js", "test", "--list",
  ], { env, encoding: "utf8", timeout: 30_000 });
  assert.ifError(result.error);
  return result;
}

const missing = { ...process.env, CI: "true" };
delete missing.E2E_RECORDED_SOURCE;
for (const value of [undefined, "", "   "]) {
  const env = { ...missing };
  if (value !== undefined) env.E2E_RECORDED_SOURCE = value;
  const result = list(env);
  assert.notEqual(result.status, 0, "CI must reject a missing/blank recorded source");
  assert.match(result.stdout + result.stderr, /E2E_RECORDED_SOURCE/);
}
const configured = list({ ...missing, E2E_RECORDED_SOURCE: "/tmp/recorded.avi" });
assert.equal(configured.status, 0, configured.stdout + configured.stderr);
assert.match(configured.stdout, /recorded-pipeline\.spec\.ts/);
const local = { ...missing };
delete local.CI;
assert.equal(list(local).status, 0, "local video acceptance remains opt-in");
console.log("E2E gate checks passed");
