const { isIPv4 } = require("node:net");

const assets = new Map([
  ["/console/console.css", "stylesheet"],
  ["/console/console.js", "script"],
]);
const loadTimeoutMs = 12000;

function serverConsoleUrl(value) {
  const origin = typeof value === "string" &&
    /^https?:\/\/[^/?#\\@\s\x00-\x1f\x7f]+\/?$/i.test(value) &&
    URL.parse(value);
  if (origin && origin.port !== "0") {
    const host = origin.hostname;
    const loopback = host === "localhost" || host === "[::1]" ||
      (isIPv4(host) && host.startsWith("127."));
    if (origin.protocol === "https:" || loopback) {
      return new URL("/console/", origin);
    }
  }
  throw new Error("The selected TeamUp profile does not name a valid Server origin.");
}

function allowsServerConsoleRequest(landing, request) {
  const target = URL.parse(request.url);
  if (!target || target.origin !== landing.origin || target.username || target.password) {
    return false;
  }
  const { method, resourceType, initiatorOrigin } = request;
  if (resourceType === "mainFrame") {
    return method === "GET" && target.href === landing.href;
  }
  if (!target.search && method === "GET" && assets.get(target.pathname) === resourceType) {
    return true;
  }
  if (resourceType !== "xhr" || initiatorOrigin !== landing.origin) {
    return false;
  }
  return (method === "POST" && target.pathname === "/v1/auth/login" && !target.search) ||
    (method === "GET" && target.pathname.startsWith("/v1/console/"));
}

function createServerConsole({ BrowserWindow, dialog, serverSession, readOrigin }) {
  let window;
  let opening;
  const block = (event) => event.preventDefault();
  serverSession.setPermissionCheckHandler(() => false);
  serverSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  serverSession.on("will-download", block);

  async function open() {
    const landing = serverConsoleUrl(await readOrigin());
    serverSession.webRequest.onBeforeRequest((request, callback) => {
      callback({ cancel: !allowsServerConsoleRequest(landing, request) });
    });
    const candidate = new BrowserWindow({
      title: "TeamUp Control Console",
      width: 1000,
      height: 800,
      minWidth: 680,
      minHeight: 520,
      show: false,
      webPreferences: {
        session: serverSession,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    window = candidate;
    candidate.once("closed", () => { window = null; });
    candidate.webContents.on("will-frame-navigate", block);
    candidate.webContents.on("will-redirect", block);
    candidate.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    let status;
    candidate.webContents.once("did-navigate", (_event, _url, code) => { status = code; });
    const pendingAssets = new Set(Array.from(assets.keys(), (path) => new URL(path, landing).href));
    serverSession.webRequest.onHeadersReceived((response, callback) => {
      if (response.statusCode === 200 || response.statusCode === 304) {
        pendingAssets.delete(response.url);
      }
      callback({});
    });

    let timeout;
    const deadline = new Promise((_resolve, reject) => {
      timeout = setTimeout(
        () => reject(new Error("Opening the Server Control Console timed out.")),
        loadTimeoutMs,
      );
    });
    try {
      await Promise.race([candidate.loadURL(landing.href), deadline]);
      if (status !== 200) {
        throw new Error("The Server did not serve its Control Console at /console/.");
      }
      if (pendingAssets.size) {
        throw new Error("The Server did not serve all Control Console assets.");
      }
      candidate.show();
    } catch (error) {
      if (candidate.isDestroyed()) return;
      candidate.destroy();
      throw error;
    } finally {
      clearTimeout(timeout);
      serverSession.webRequest.onHeadersReceived(null);
    }
  }

  return function showServerConsole() {
    if (opening) return;
    if (window) {
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
      return;
    }
    opening = open().catch((error) => {
      dialog.showErrorBox(
        "Could not open the Server Control Console",
        error.message + "\n\nCheck the configured Server and its connection, then try again.",
      );
    }).finally(() => { opening = null; });
  };
}

module.exports = { serverConsoleUrl, allowsServerConsoleRequest, createServerConsole };
