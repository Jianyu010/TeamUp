const { app, BrowserWindow, Menu, dialog, ipcMain, protocol, safeStorage, session } = require("electron");
const { execFile } = require("node:child_process");
const { userInfo } = require("node:os");
const { join, resolve } = require("node:path");
const { promisify } = require("node:util");
const { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint, readAdminAccess, requestAdminSession } = require("./console-protocol.cjs");

const {createAdminWindow} = require("./admin-window.cjs");
const {createDesktopLogin} = require("./console-login.cjs");
const {createKeychain} = require("./keychain.cjs");

const exec = promisify(execFile);
const home = userInfo().homedir;
const development = app.isPackaged ? {} : process.env;
const runtime = resolve(development.TEAMUP_DESKTOP_RUNTIME ||
  join(home, ".local/share/teamup/clients/current/.venv/bin"));
const profile = resolve(development.TEAMUP_DESKTOP_CONFIG ||
  join(home, ".config/teamup/client.json"));
const stateDirectory = resolve(development.TEAMUP_DESKTOP_STATE_DIR ||
  join(home, "Library/Application Support/TeamUp Launcher"));
const assetDirectory = app.isPackaged
  ? join(__dirname, "console")
  : join(__dirname, "../../shared/src/teamup_client/console");
const launcher = join(app.isPackaged ? process.resourcesPath : __dirname, "launch.py");
const keychainHelper = join(app.isPackaged ? process.resourcesPath : join(__dirname, "dist"), "teamup-keychain");
const desktopLogin = createDesktopLogin(createKeychain(keychainHelper, safeStorage), profile);

protocol.registerSchemesAsPrivileged([{
  scheme: "teamup",
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
}]);

let endpointPromise;
let window;
let opening;
let consoleSession;
let adminWindow;

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
  const endpoint = parseConsoleEndpoint(stdout);
  await desktopLogin.restore(endpoint);
  return endpoint;
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
  const preparing = getEndpoint();
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
    showAdminMenu(false);
  });
  consoleWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  consoleWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  consoleWindow.once("ready-to-show", () => consoleWindow.show());
  await Promise.all([consoleWindow.loadURL(CONSOLE_URL), preparing]);
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

async function forgetSavedCredentials() {
  try {
    await desktopLogin.forget();
    await dialog.showMessageBox({message: "Saved TeamUp credentials removed."});
  } catch {
    await dialog.showMessageBox({
      type: "warning",
      message: "TeamUp could not remove saved credentials from Keychain.",
    });
  }
}

function showAdminMenu(visible) {
  const item = Menu.getApplicationMenu()?.getMenuItemById("admin");
  if (item) item.visible = visible;
}

async function openAdmin(access) {
  try {
    await adminWindow.open(access);
  } catch (error) {
    dialog.showErrorBox("Could not open Admin", "Check the configured Server and its connection, then try again.");
    throw error;
  }
}

async function openAdminFromMenu() {
  let access;
  try {
    access = await readAdminAccess(await getEndpoint());
  } catch {}
  if (access) {
    await openAdmin(access).catch(() => {});
  } else {
    showAdminMenu(false);
    dialog.showErrorBox("Could not open Admin", "Check your Server connection and TeamUp sign-in.");
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
    adminWindow = createAdminWindow(
      BrowserWindow, session.fromPartition("teamup-admin", {cache: false}),
      async (access) => requestAdminSession(await getEndpoint(), access),
    );
    ipcMain.handle("teamup:admin-sign-in", (event) => adminWindow.signIn(event));
    consoleSession.setPermissionCheckHandler(() => false);
    consoleSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    consoleSession.protocol.handle(
      "teamup", createConsoleHandler(assetDirectory, getEndpoint, resetEndpoint, desktopLogin, {
        show: showAdminMenu,
        open: openAdmin,
      }),
    );
    if (process.platform === "darwin") {
      if (app.isPackaged) {
        app.setAboutPanelOptions({
          applicationVersion: require("./package.json").teamupVersion,
          version: "",
        });
      }
      Menu.setApplicationMenu(Menu.buildFromTemplate([
        {label: app.name, submenu: [
          {role: "about"},
          {label: "Forget Saved Credentials", click: forgetSavedCredentials},
          {type: "separator"},
          {role: "services"},
          {type: "separator"},
          {role: "hide"},
          {role: "hideOthers"},
          {role: "unhide"},
          {type: "separator"},
          {role: "quit"},
        ]},
        {role: "editMenu"},
        {label: "View", submenu: [
          {id: "admin", label: "Admin", visible: false, click: openAdminFromMenu},
          {role: "reload"},
          {role: "forceReload"},
          {role: "toggleDevTools"},
          {type: "separator"},
          {role: "resetZoom"},
          {role: "zoomIn"},
          {role: "zoomOut"},
          {type: "separator"},
          {role: "togglefullscreen"},
        ]},
        {role: "windowMenu"},
      ]));
    }
    app.on("activate", showConsole);
    app.on("second-instance", showConsole);
    showConsole();
  });
}
