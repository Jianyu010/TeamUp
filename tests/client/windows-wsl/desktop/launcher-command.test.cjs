const assert = require("node:assert/strict");
const test = require("node:test");
const { distributionArguments, launchConsole } = require("../../../../client/windows-wsl/desktop/launcher-command.cjs");

function windowsRun() {
  const calls = [];
  return {
    calls,
    run: async (file, args, options) => {
      calls.push({ file, args, options });
      if (args.includes("wslpath")) return { stdout: args.at(-1).endsWith("launch-wsl.py")
        ? "/mnt/c/Program Files/TeamUp/launch-wsl.py\n"
        : "/mnt/c/Program Files/TeamUp/launch.py\n" };
      return { stdout: "private-launch-result" };
    },
  };
}

test("Windows uses argument arrays and preserves WSL defaults instead of Windows path resolution", async () => {
  const fake = windowsRun();
  const result = await launchConsole({ platform: "win32", environment: {},
    launcher: "C:\\Program Files\\TeamUp\\launch.py", windowsLauncher: "C:\\Program Files\\TeamUp\\launch-wsl.py",
    run: fake.run });
  assert.equal(result.stdout, "private-launch-result");
  assert.equal(fake.calls.length, 3);
  const call = fake.calls[2];
  assert.equal(call.file, "wsl.exe");
  assert.deepEqual(call.args, ["--exec", "python3", "-I", "/mnt/c/Program Files/TeamUp/launch-wsl.py",
    "--launcher", "/mnt/c/Program Files/TeamUp/launch.py",
    "--runtime", "~/.local/share/teamup/clients/current/.venv/bin",
    "--config", "~/.config/teamup/client.json", "--state-dir", "~/.local/state/teamup/launcher"]);
  assert.equal(call.options.windowsHide, true);
  assert.equal(call.options.shell, undefined);
});

test("WSL distribution and custom paths with spaces or shell metacharacters remain literal arguments", async () => {
  const fake = windowsRun();
  const profile = "/home/alice/Team Up/$(not-a-command); client.json";
  await launchConsole({ platform: "win32", environment: {
    TEAMUP_DESKTOP_WSL_DISTRO: "Ubuntu Work", TEAMUP_DESKTOP_CONFIG: profile,
    TEAMUP_DESKTOP_RUNTIME: "/home/alice/Runtime Bin", TEAMUP_DESKTOP_STATE_DIR: "/home/alice/Launcher State",
  }, launcher: "launch.py", windowsLauncher: "launch-wsl.py", run: fake.run });
  for (const call of fake.calls) assert.deepEqual(call.args.slice(0, 2), ["--distribution", "Ubuntu Work"]);
  assert.equal(fake.calls[2].args[fake.calls[2].args.indexOf("--config") + 1], profile);
  assert.equal(fake.calls[2].args[fake.calls[2].args.indexOf("--runtime") + 1], "/home/alice/Runtime Bin");
});

test("invalid WSL selection is rejected before launching anything", async () => {
  for (const value of ["--exec", "Ubuntu\nother", "Ubuntu\0other", 42]) {
    await assert.rejects(launchConsole({ platform: "win32", environment: { TEAMUP_DESKTOP_WSL_DISTRO: value },
      launcher: "a", windowsLauncher: "b", run: () => assert.fail("must not launch") }), /installed WSL distribution/);
  }
  assert.deepEqual(distributionArguments(undefined), []);
});

test("invalid path conversion fails without reflecting the returned private text", async () => {
  const privateValue = "http://127.0.0.1:1234/#private";
  await assert.rejects(launchConsole({ platform: "win32", environment: {}, launcher: "a", windowsLauncher: "b",
    run: async () => ({ stdout: privateValue }) }), (error) => {
    assert.match(error.message, /translate/);
    assert.ok(!error.message.includes(privateValue));
    return true;
  });
});

test("macOS retains its native runtime, profile and launcher state defaults", async () => {
  const calls = [];
  await launchConsole({ platform: "darwin", environment: {}, home: "/Users/alice",
    launcher: "/Applications/TeamUp.app/Contents/Resources/launch.py", run: async (...args) => { calls.push(args); } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/Users/alice/.local/share/teamup/clients/current/.venv/bin/python");
  assert.deepEqual(calls[0][1], ["-I", "/Applications/TeamUp.app/Contents/Resources/launch.py",
    "--config", "/Users/alice/.config/teamup/client.json",
    "--client", "/Users/alice/.local/share/teamup/clients/current/.venv/bin/teamup-client",
    "--state-dir", "/Users/alice/Library/Application Support/TeamUp Launcher"]);
});

test("native runtime overrides keep their existing meaning", async () => {
  let command;
  await launchConsole({ platform: "darwin", environment: { TEAMUP_DESKTOP_RUNTIME: "/runtime/bin",
    TEAMUP_DESKTOP_CONFIG: "/private/profile.json", TEAMUP_DESKTOP_STATE_DIR: "/private/launcher" },
    launcher: "/app/launch.py", run: async (...args) => { command = args; } });
  assert.equal(command[0], "/runtime/bin/python");
  assert.ok(command[1].includes("/private/profile.json"));
  assert.ok(command[1].includes("/private/launcher"));
});
