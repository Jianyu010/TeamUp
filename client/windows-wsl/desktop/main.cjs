const { app, BrowserWindow, dialog, protocol, session } = require("electron");
const { join } = require("node:path");
const { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint } = require("./console-protocol.cjs");
const { launchConsole } = require("./launcher-command.cjs");

const assetDirectory = app.isPackaged
  ? join(__dirname, "console")
  : join(__dirname, "../../shared/src/teamup_client/console");
const launcher = join(app.isPackaged ? process.resourcesPath : __dirname, "launch.py");
const windowsLauncher = join(app.isPackaged ? process.resourcesPath : __dirname, "launch-wsl.py");

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
    ({ stdout } = await launchConsole({ launcher, windowsLauncher }));
  } catch (error) {
    const detail = error.code === 1
      ? error.stderr?.trim()
      : process.platform === "win32"
        ? "Check WSL2, Python 3 and the installed TeamUp Client profile. See the Windows setup instructions."
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
    icon: join(assetDirectory, "jet-pair.png"),
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
    if (process.platform === "win32") app.setAppUserModelId("org.teamup.desktop");
    consoleSession = session.fromPartition("teamup-console");
    consoleSession.setPermissionCheckHandler(() => false);
    consoleSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    consoleSession.protocol.handle("teamup", createConsoleHandler(assetDirectory, getEndpoint, resetEndpoint));
    app.on("activate", showConsole);
    app.on("second-instance", showConsole);
    showConsole();
  });
}
