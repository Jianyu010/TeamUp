const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const test = require("node:test");

const installer = resolve(__dirname, "../../../../client/windows-wsl/desktop/install-win.ps1");
function fixture() {
  const root = mkdtempSync(join(tmpdir(), "teamup-install-test-"));
  const source = join(root, "portable package");
  mkdirSync(join(source, "resources"), { recursive: true });
  for (const file of ["TeamUp.exe", "resources/app.asar", "resources/launch.py", "resources/launch-wsl.py"]) {
    writeFileSync(join(source, file), "new payload");
  }
  return { root, source, destination: join(root, "TeamUp"), shortcut: join(root, "TeamUp.lnk") };
}
function install(value, destination = value.destination) {
  return execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-File", installer,
    "-SourceDirectory", value.source, "-InstallDirectory", destination, "-ShortcutPath", value.shortcut],
  { encoding: "utf8", windowsHide: true, timeout: 30000 });
}

test("Windows installer upgrades a complete portable app and preserves the previous installation", {
  skip: process.platform !== "win32",
}, () => {
  const value = fixture();
  mkdirSync(value.destination);
  writeFileSync(join(value.destination, "previous-marker"), "keep for rollback");
  const receipt = JSON.parse(install(value));
  assert.equal(readFileSync(join(value.destination, "resources/app.asar"), "utf8"), "new payload");
  assert.equal(readFileSync(join(receipt.previous, "previous-marker"), "utf8"), "keep for rollback");
  assert.equal(realpathSync.native(receipt.installed), realpathSync.native(value.destination));
  assert.ok(readFileSync(value.shortcut).length > 0);
  assert.deepEqual(readdirSync(value.root).filter((name) => name.startsWith("TeamUp.installing-")), []);
});

test("Windows installer refuses an incomplete package without altering the installed app", {
  skip: process.platform !== "win32",
}, () => {
  const value = fixture();
  value.source = join(value.root, "incomplete");
  mkdirSync(value.source);
  mkdirSync(value.destination);
  writeFileSync(join(value.destination, "previous-marker"), "unchanged");
  assert.throws(() => install(value), /incomplete/);
  assert.equal(readFileSync(join(value.destination, "previous-marker"), "utf8"), "unchanged");
  assert.deepEqual(readdirSync(value.root).filter((name) => name.startsWith("TeamUp.previous-")), []);
});

test("Windows installer rejects an unrelated target directory before making changes", {
  skip: process.platform !== "win32",
}, () => {
  const value = fixture();
  assert.throws(() => install(value, join(value.root, "unrelated")), /must be named TeamUp/);
  assert.deepEqual(readdirSync(value.root), ["portable package"]);
});
