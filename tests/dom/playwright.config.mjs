import { defineConfig, devices } from "playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";


const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
const port = Number(process.env.CULLUMI_DOM_PORT || 4173);
const token = process.env.CULLUMI_DOM_TOKEN || "cullumi-dom-test";
const python = process.env.CULLUMI_PYTHON || path.join(root, ".venv", "Scripts", "python.exe");
const server = path.join(root, "tests", "dom_server.py");
const quoted = value => `"${value.replaceAll('"', '\\"')}"`;

export default defineConfig({
  globalTeardown: path.join(here, "global-teardown.mjs"),
  testDir: here,
  testMatch: "*.spec.mjs",
  fullyParallel: false,
  workers: 1,
  timeout: 15_000,
  expect: {
    timeout: 5_000,
    // Edge rasterization can shift a few anti-aliased SVG pixels between
    // patch releases. Keep the tolerance tiny while preserving useful diffs.
    toHaveScreenshot: { maxDiffPixels: 100 },
  },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${quoted(python)} ${quoted(server)}`,
    cwd: root,
    env: {
      ...process.env,
      CULLUMI_DOM_PORT: String(port),
      CULLUMI_DOM_TOKEN: token,
    },
    url: `http://127.0.0.1:${port}/?token=${token}`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [
    {
      name: "edge",
      use: { ...devices["Desktop Edge"], channel: "msedge" },
    },
  ],
});
