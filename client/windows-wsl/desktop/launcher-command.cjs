const { execFile } = require("node:child_process");
const { homedir } = require("node:os");
const { posix: { join, resolve } } = require("node:path");
const { promisify } = require("node:util");

const execute = promisify(execFile);
const options = { timeout: 35000, maxBuffer: 4096, windowsHide: true };

function distributionArguments(value) {
  if (value === undefined || value === "") return [];
  if (typeof value !== "string" || value.startsWith("-") || /[\0\r\n]/.test(value)) {
    throw new Error("TEAMUP_DESKTOP_WSL_DISTRO must name an installed WSL distribution.");
  }
  return ["--distribution", value];
}

async function launchConsole({
  launcher,
  windowsLauncher,
  platform = process.platform,
  environment = process.env,
  home = homedir(),
  run = execute,
}) {
  if (platform === "win32") {
    const distribution = distributionArguments(environment.TEAMUP_DESKTOP_WSL_DISTRO);
    const translate = async (file) => {
      const { stdout } = await run("wsl.exe", [...distribution, "--exec", "wslpath", "-u", file], options);
      const translated = stdout.trim();
      if (!translated.startsWith("/") || /[\0\r\n]/.test(translated)) {
        throw new Error("WSL could not translate the TeamUp launcher path.");
      }
      return translated;
    };
    const nativeLauncher = await translate(launcher);
    const bridge = await translate(windowsLauncher);
    return run("wsl.exe", [
      ...distribution, "--exec", "python3", "-I", bridge,
      "--launcher", nativeLauncher,
      "--runtime", environment.TEAMUP_DESKTOP_RUNTIME || "~/.local/share/teamup/clients/current/.venv/bin",
      "--config", environment.TEAMUP_DESKTOP_CONFIG || "~/.config/teamup/client.json",
      "--state-dir", environment.TEAMUP_DESKTOP_STATE_DIR || "~/.local/state/teamup/launcher",
    ], options);
  }
  const runtime = resolve(environment.TEAMUP_DESKTOP_RUNTIME ||
    join(home, ".local/share/teamup/clients/current/.venv/bin"));
  const profile = resolve(environment.TEAMUP_DESKTOP_CONFIG || join(home, ".config/teamup/client.json"));
  const state = resolve(environment.TEAMUP_DESKTOP_STATE_DIR || join(home, "Library/Application Support/TeamUp Launcher"));
  return run(join(runtime, "python"), [
    "-I", launcher, "--config", profile, "--client", join(runtime, "teamup-client"),
    "--state-dir", state,
  ], options);
}

module.exports = { distributionArguments, launchConsole };
