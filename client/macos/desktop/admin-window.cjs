// The Server console runs in its own Electron session, with only its published
// routes and the public CI frame reachable from that renderer.
const ciOrigin = "https://jianyu010.github.io";
const ciRoot = ciOrigin + "/TeamUp/";
const ciPages = new Set(["", "index.html", "ci.html", "repo.html", "performance.html", "agents.html"]
  .map((path) => ciRoot + path));
const ciResources = new Map([
  [ciRoot + "assets/style.css", "stylesheet"],
  [ciRoot + "assets/app.js", "script"],
  ["https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js", "script"],
  ...["nightly", "weekly", "repo", "releases", "ci"]
    .map((name) => [ciRoot + `data/${name}.jsonl`, "xhr"]),
]);
const adminAssets = new Map([
  ["/console/console.css", "stylesheet"],
  ["/console/console.js", "script"],
  ["/console/accounts.js", "script"],
  ["/console/maintenance.js", "script"],
]);
const adminReads = new Set([
  "/v1/console/capabilities",
  "/v1/console/accounts",
  "/v1/console/maintenance/health",
  "/v1/console/maintenance/logs",
  "/v1/console/rooms",
]);
const adminRoomRead = /^\/v1\/console\/rooms\/[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}(?:\/(?:load|timeline|transcript))?$/i;
const adminAccountWrite = /^\/v1\/console\/accounts\/[a-z][a-z0-9_-]{2,63}\/(?:password|disable)$/;

function allowsAdminRequest(landing, {url, method, resourceType, initiatorOrigin}) {
  const target = URL.parse(url);
  if (!target || target.username || target.password) return false;
  if (resourceType === "mainFrame") {
    return method === "GET" && target.href === landing.href;
  }
  if (resourceType === "subFrame") {
    return method === "GET" && ciPages.has(target.href) &&
      (initiatorOrigin === landing.origin || initiatorOrigin === ciOrigin);
  }
  if (initiatorOrigin === ciOrigin) {
    return method === "GET" && ciResources.get(target.href) === resourceType;
  }
  if (initiatorOrigin !== landing.origin || target.origin !== landing.origin) return false;
  const {pathname, search} = target;
  if (method === "GET" && !search && adminAssets.get(pathname) === resourceType) return true;
  if (resourceType !== "xhr") return false;
  if (method === "GET") return adminReads.has(pathname) || adminRoomRead.test(pathname);
  return method === "POST" && !search && (
    pathname === "/v1/auth/login" || pathname === "/v1/console/accounts" ||
    adminAccountWrite.test(pathname)
  );
}

async function load(window, url) {
  let status;
  window.webContents.once("did-navigate", (_event, _url, code) => { status = code; });
  let timer;
  try {
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("The Server Console took too long to load.")), 12000);
    });
    await Promise.race([window.loadURL(url), deadline]);
    if (status !== 200) throw new Error("The Server Console could not be loaded.");
    window.show();
  } catch (error) {
    if (window.isDestroyed()) return;
    window.destroy();
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function createAdminWindow(BrowserWindow, adminSession) {
  let current;
  adminSession.setPermissionCheckHandler(() => false);
  adminSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  adminSession.on("will-download", (event) => event.preventDefault());

  return async function openAdminWindow(url) {
    if (current?.url === url) {
      if (current.loading) return current.loading;
      const window = current.window;
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
      return;
    }

    current?.window.destroy();
    const landing = new URL(url);
    adminSession.webRequest.onBeforeRequest((details, callback) => {
      callback({cancel: !allowsAdminRequest(landing, details)});
    });
    const window = new BrowserWindow({
      title: "TeamUp Admin",
      width: 1200,
      height: 800,
      minWidth: 720,
      minHeight: 520,
      show: false,
      webPreferences: {
        session: adminSession,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    window.webContents.on("will-frame-navigate", (event) => {
      const allowed = event.isMainFrame ? event.url === landing.href : ciPages.has(event.url);
      if (!allowed) event.preventDefault();
    });
    window.webContents.on("will-redirect", (event) => event.preventDefault());
    window.webContents.setWindowOpenHandler(() => ({action: "deny"}));

    const view = {url, window, loading: null};
    current = view;
    window.once("closed", () => {
      if (current === view) current = null;
    });
    view.loading = load(window, landing.href).finally(() => { view.loading = null; });
    return view.loading;
  };
}

module.exports = {allowsAdminRequest, createAdminWindow};
