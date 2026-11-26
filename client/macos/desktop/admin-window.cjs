const http = require("node:http");
const { Readable } = require("node:stream");
const { join } = require("node:path");

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

async function serveThroughSocket(request, landing, serverSocket) {
  const target = new URL(request.url);
  const resourceType = target.href === landing.href ? "mainFrame" :
    (adminAssets.get(target.pathname) ?? "xhr");
  const {method, url} = request;
  if (target.origin !== landing.origin ||
      !allowsAdminRequest(landing, {url, method, resourceType, initiatorOrigin: landing.origin})) {
    return new Response(null, {status: 403});
  }

  try {
    const headers = {Host: target.host};
    for (const name of ["Accept", "Content-Type", "Authorization"]) {
      const value = request.headers.get(name);
      if (value && resourceType === "xhr") headers[name] = value;
    }
    const body = request.body ? Buffer.from(await request.arrayBuffer()) : undefined;
    if (body && body.length > 128 * 1024) return new Response(null, {status: 413});
    if (body) headers["Content-Length"] = body.length;
    const response = await new Promise((resolve, reject) => {
      const upstream = http.request({
        socketPath: serverSocket, path: target.pathname + target.search, method, headers,
        agent: false, signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]),
      }, resolve);
      upstream.once("error", reject);
      upstream.end(body);
    });
    if (response.statusCode >= 300 && response.statusCode < 400) {
      response.destroy();
      return new Response(null, {status: 502});
    }
    const responseHeaders = new Headers();
    for (const name of ["content-type", "content-security-policy", "cache-control",
      "referrer-policy", "x-content-type-options"]) {
      const value = response.headers[name];
      if (typeof value === "string") responseHeaders.set(name, value);
    }
    return new Response(Readable.toWeb(response), {
      status: response.statusCode, headers: responseHeaders,
    });
  } catch {
    return new Response(null, {status: 502});
  }
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

function createAdminWindow(BrowserWindow, adminSession, issueSession) {
  let current;
  adminSession.setPermissionCheckHandler(() => false);
  adminSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  adminSession.on("will-download", (event) => event.preventDefault());

  async function open(access) {
    const {url, serverSocket} = access;
    if (current?.url === url && current.access.serverSocket === serverSocket) {
      if (current.loading) return current.loading;
      const window = current.window;
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
      return;
    }

    current?.window.destroy();
    const landing = new URL(url);
    const automatic = Boolean(serverSocket) || landing.protocol === "https:";
    if (serverSocket) {
      adminSession.protocol.handle("http", (request) => serveThroughSocket(request, landing, serverSocket));
    }
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
        ...(automatic ? {preload: join(__dirname, "admin-preload.cjs")} : {}),
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

    const view = {url, access, automatic, window, loading: null};
    current = view;
    window.once("closed", () => {
      if (current === view) {
        if (serverSocket) adminSession.protocol.unhandle("http");
        current = null;
      }
    });
    view.loading = load(window, landing.href).finally(() => { view.loading = null; });
    return view.loading;
  }

  async function signIn({sender, senderFrame}) {
    const view = current;
    if (!view?.automatic || sender !== view.window.webContents || senderFrame?.parent !== null ||
        senderFrame.url !== view.url) {
      return {status: 403, payload: null};
    }
    const result = await issueSession(view.access);
    return current === view ? result : {status: 403, payload: null};
  }

  return {open, signIn};
}

module.exports = {allowsAdminRequest, createAdminWindow};
