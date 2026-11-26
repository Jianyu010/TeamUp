const { app, BrowserWindow, dialog, protocol, session } = require("electron");
const { execFile } = require("node:child_process");
const { homedir } = require("node:os");
const { join, resolve } = require("node:path");
const { promisify } = require("node:util");
const { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint } = require("./console-protocol.cjs");

const exec = promisify(execFile);
const runtime = resolve(process.env.TEAMUP_DESKTOP_RUNTIME ||
  join(homedir(), ".local/share/teamup/clients/current/.venv/bin"));
const profile = resolve(process.env.TEAMUP_DESKTOP_CONFIG ||
  join(homedir(), ".config/teamup/client.json"));
const stateDirectory = resolve(process.env.TEAMUP_DESKTOP_STATE_DIR ||
  join(homedir(), "Library/Application Support/TeamUp Launcher"));
const assetDirectory = app.isPackaged
  ? join(__dirname, "console")
  : join(__dirname, "../src/teamup_client/console");
const launcher = join(app.isPackaged ? process.resourcesPath : __dirname, "launch.py");

protocol.registerSchemesAsPrivileged([{
  scheme: "teamup",
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
}]);

let endpointPromise;
let window;
let opening;
let consoleSession;

async function prepareConsole() {
  let stdout;
  try {
    ({ stdout } = await exec(join(runtime, "python"), [
      "-I", launcher, "--config", profile, "--client", join(runtime, "teamup-client"),
      "--state-dir", stateDirectory,
    ], { timeout: 35000, maxBuffer: 4096 }));
  } catch (error) {
    const detail = error.code === 1
      ? error.stderr.trim()
      : "Check the installed TeamUp Client runtime and try opening TeamUp again.";
    throw new Error(detail || "TeamUp could not start the local Client console.");
  }
  return parseConsoleEndpoint(stdout);
}

function resetEndpoint(stale) {
  if (endpointPromise === stale) endpointPromise = null;
}

function getEndpoint() {
  if (!endpointPromise) {
    const pending = prepareConsole();
    endpointPromise = pending;
    pending.catch(() => resetEndpoint(pending));
  }
  return endpointPromise;
}

async function openConsole() {
  await getEndpoint();
  const consoleWindow = new BrowserWindow({
    title: "TeamUp",
    width: 1200,
    height: 800,
    minWidth: 720,
    minHeight: 520,
    show: false,
    webPreferences: {
      session: consoleSession,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  window = consoleWindow;
  consoleWindow.once("closed", () => {
    window = null;
    endpointPromise = null;
  });
  consoleWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  consoleWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  consoleWindow.once("ready-to-show", () => consoleWindow.show());
  await consoleWindow.loadURL(CONSOLE_URL);
}

function showConsole() {
  if (window) {
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  } else if (!opening) {
    opening = openConsole().catch((error) => {
      dialog.showErrorBox("Could not open TeamUp", error.message);
      app.quit();
    }).finally(() => { opening = null; });
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.whenReady().then(() => {
    consoleSession = session.fromPartition("teamup-console");
    consoleSession.setPermissionCheckHandler(() => false);
    consoleSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    consoleSession.protocol.handle("teamup", createConsoleHandler(assetDirectory, getEndpoint, resetEndpoint));
    app.on("activate", showConsole);
    app.on("second-instance", showConsole);
    showConsole();
  });
}
