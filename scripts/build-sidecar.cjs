#!/usr/bin/env node
/**
 * Cross-platform sidecar build dispatcher.
 *
 * Delegates to the platform-appropriate build script:
 *   Windows → sidecar/build.ps1  (PowerShell)
 *   macOS   → sidecar/build.sh   (bash)
 *   Linux   → sidecar/build.sh   (bash)
 *
 * Usage: node scripts/build-sidecar.js [--clean]
 */
const { spawnSync } = require("child_process");
const os = require("os");
const path = require("path");

const args = process.argv.slice(2);
const repoRoot = path.resolve(__dirname, "..");

let cmd, cmdArgs;

if (os.platform() === "win32") {
  cmd = "powershell";
  cmdArgs = [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(repoRoot, "sidecar", "build.ps1"),
    ...args.map((a) => (a === "--clean" ? "-Clean" : a)),
  ];
} else {
  cmd = "bash";
  cmdArgs = [path.join(repoRoot, "sidecar", "build.sh"), ...args];
}

console.log(`[build-sidecar] Running: ${cmd} ${cmdArgs.join(" ")}`);

const result = spawnSync(cmd, cmdArgs, { stdio: "inherit", cwd: repoRoot });

if (result.status !== 0) {
  process.exit(result.status ?? 1);
}
