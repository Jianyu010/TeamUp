const { readFile } = require("node:fs/promises");
const http = require("node:http");
const { join } = require("node:path");
const { Readable } = require("node:stream");

const CONSOLE_ORIGIN = "teamup://console";
const CONSOLE_URL = CONSOLE_ORIGIN + "/";
const assets = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/console.css", ["console.css", "text/css; charset=utf-8"]],
  ["/console.js", ["console.js", "text/javascript; charset=utf-8"]],
  ["/console-history.mjs", ["console-history.mjs", "text/javascript; charset=utf-8"]],
  ["/console-members.js", ["console-members.js", "text/javascript; charset=utf-8"]],
  ["/console-board.js", ["console-board.js", "text/javascript; charset=utf-8"]],
  ["/console-tasks.js", ["console-tasks.js", "text/javascript; charset=utf-8"]],
  ["/console-sessions.js", ["console-sessions.js", "text/javascript; charset=utf-8"]],
  ["/jet-pair.png", ["jet-pair.png", "image/png"]],
]);
const reads = new Set([
  "/api/workspace", "/api/access", "/api/login", "/api/wake-settings", "/api/versions", "/api/retired", "/api/team", "/api/room", "/api/room-members",
  "/api/room-board", "/api/tasks", "/api/task-events",
]);
const taskAction = /^\/api\/tasks\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\/(?:join|progress|finish)$/i;
const actions = new Set([
  "/api/login", "/api/wake-settings", "/api/sessions/leave", "/api/sessions/retire", "/api/room-board", "/api/room-members/remove",
]);
const requestLimit = 128 * 1024;
const headers = {
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

function failure(status, state) {
  return Response.json({ state }, { status, headers });
}

function parseConsoleEndpoint(value) {
  const [url, loginSocket, ...extra] = value.trim().split("\n");
  const match = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/#([A-Za-z0-9_-]{43})$/.exec(url);
  const port = Number(match?.[1]);
  if (!match || port < 1 || port > 65535 || extra.length ||
      (loginSocket !== undefined && !/^\/tmp\/teamup-console-[a-z0-9_]+\/login\.sock$/.test(loginSocket))) {
    throw new Error("TeamUp did not receive a valid Client console address.");
  }
  return { port, token: match[2], ...(loginSocket ? { loginSocket } : {}) };
}

async function forward(request, target, { port, token, loginSocket }, privateSocket = false) {
  const usesLoginSocket = privateSocket || (request.method === "POST" && target.pathname === "/api/login");
  if (usesLoginSocket && !loginSocket) return failure(503, "unavailable");
  const clientHeaders = {
    Host: `127.0.0.1:${port}`,
    Authorization: `Bearer ${token}`,
    Origin: `http://127.0.0.1:${port}`,
  };
  let body;
  if (request.method === "POST") {
    body = Buffer.from(await request.arrayBuffer());
    if (body.length > requestLimit) return failure(413, "invalid_request");
    if (body.length) {
      if (request.headers.get("Content-Type") !== "application/json") {
        return failure(400, "invalid_request");
      }
      clientHeaders["Content-Type"] = "application/json";
    }
    clientHeaders["Content-Length"] = body.length;
  }
  const response = await new Promise((resolve, reject) => {
    const upstream = http.request({
      ...(usesLoginSocket ? { socketPath: loginSocket } : { hostname: "127.0.0.1", port }),
      path: target.pathname + target.search,
      method: request.method,
      headers: clientHeaders,
      agent: false,
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(20000)]),
    }, resolve);
    upstream.once("error", reject);
    upstream.end(body);
  });
  const { statusCode } = response;
  if ((statusCode !== 200 && statusCode < 400) ||
      response.headers["content-type"]?.split(";", 1)[0] !== "application/json") {
    response.destroy();
    return failure(502, "unavailable");
  }
  return new Response(Readable.toWeb(response), {
    status: statusCode,
    headers: { ...headers, "Content-Type": "application/json; charset=utf-8" },
  });
}

async function requestPrivateLogin(endpoint, credentials, signal) {
  const target = new URL("/api/login", CONSOLE_URL);
  const request = new Request(target, {
    method: credentials === undefined ? "GET" : "POST",
    headers: credentials === undefined ? undefined : {"Content-Type": "application/json"},
    body: credentials === undefined ? undefined : JSON.stringify(credentials),
    signal,
  });
  const response = await forward(request, target, endpoint, true);
  return {status: response.status, result: await response.json()};
}

async function signIn(request, endpoint, desktopLogin) {
  if (request.headers.get("Content-Type") !== "application/json") {
    return failure(400, "invalid_request");
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > requestLimit) return failure(413, "invalid_request");
  let credentials;
  try {
    credentials = JSON.parse(Buffer.from(bytes));
  } catch {
    return failure(400, "invalid_request");
  }
  if (!credentials || Object.keys(credentials).some(key => !["username", "password", "remember"].includes(key))
      || typeof credentials.username !== "string" || typeof credentials.password !== "string"
      || ("remember" in credentials && typeof credentials.remember !== "boolean")) {
    return failure(400, "invalid_request");
  }
  const {status, result} = await desktopLogin.signIn(endpoint, credentials, request.signal);
  return Response.json(result, {status, headers});
}

function adminUrl(value) {
  if (typeof value !== "string") return null;
  const url = URL.parse(value);
  if (!url || url.pathname !== "/console/" || url.username || url.password ||
      url.search || url.hash) return null;
  const loopback = ["localhost", "[::1]"].includes(url.hostname) ||
    /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
  return url.protocol === "https:" || (url.protocol === "http:" && loopback) ? url.href : null;
}

async function readAdminAccess(endpoint, signal) {
  if (!endpoint) return null;
  const target = new URL("/api/access", CONSOLE_URL);
  const response = await forward(new Request(target, {signal}), target, endpoint, Boolean(endpoint.loginSocket));
  const data = await response.json();
  const url = response.ok && adminUrl(data.admin_url);
  if (!url) return null;
  const socket = data.server_socket;
  if (socket !== undefined && (typeof socket !== "string" || !socket.startsWith("/") ||
      socket.includes("\0") || !url.startsWith("http://"))) return null;
  return {url, serverSocket: socket};
}

async function requestAdminSession(endpoint, {url, serverSocket}) {
  if (!endpoint?.loginSocket) return {status: 503, payload: null};
  const target = new URL("/api/admin/session", CONSOLE_URL);
  try {
    const response = await forward(new Request(target, {method: "POST"}), target, endpoint, true);
    const data = await response.json();
    if (!response.ok) return {status: response.status, payload: null};
    if (adminUrl(data.admin_url) !== url || (data.server_socket ?? undefined) !== serverSocket) {
      return {status: 403, payload: null};
    }
    const {access_token, username, expires_at} = data.session;
    if (typeof access_token !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(access_token) ||
        typeof username !== "string" || !/^[a-z][a-z0-9_-]{2,63}$/.test(username) ||
        !Number.isSafeInteger(expires_at)) return {status: 503, payload: null};
    return {status: 200, payload: {access_token, username, expires_at}};
  } catch {
    return {status: 503, payload: null};
  }
}

function createConsoleHandler(directory, getEndpoint, resetEndpoint = () => {}, desktopLogin, admin) {
  return async (request) => {
    const target = new URL(request.url);
    if (target.protocol !== "teamup:" || target.host !== "console" || target.username || target.password) {
      return failure(404, "not_found");
    }
    const asset = assets.get(target.pathname);
    if (request.method === "GET" && asset && !target.search) {
      const [filename, contentType] = asset;
      return new Response(await readFile(join(directory, filename)), {
        headers: { ...headers, "Content-Type": contentType },
      });
    }
    const opensAdmin = admin && request.method === "POST" && target.pathname === "/api/open-admin" && !target.search;
    const isRead = request.method === "GET" && reads.has(target.pathname);
    const isAction = request.method === "POST" &&
      (actions.has(target.pathname) || taskAction.test(target.pathname));
    if (!isRead && !isAction && !opensAdmin) return failure(404, "not_found");
    if (request.initiatorOrigin !== CONSOLE_ORIGIN) return failure(403, "forbidden");

    const pending = getEndpoint();
    const refreshesEndpoint = target.pathname === "/api/workspace" || target.pathname === "/api/versions";
    try {
      const endpoint = await pending;
      if (!endpoint) return failure(503, "unavailable");
      if (admin && (target.pathname === "/api/access" || opensAdmin)) {
        if (target.search || (opensAdmin && request.body)) return failure(400, "invalid_request");
        const access = await readAdminAccess(endpoint, request.signal);
        admin.show(Boolean(access));
        if (opensAdmin) {
          if (!access) return failure(403, "forbidden");
          try {
            await admin.open(access);
          } catch {
            return failure(503, "unavailable");
          }
          return Response.json({state: "opened"}, {headers});
        }
        return Response.json(access ? {admin_url: access.url} : {}, {headers});
      }
      if (desktopLogin && target.pathname === "/api/login" && !target.search) {
        if (isAction) return await signIn(request, endpoint, desktopLogin);
        if (endpoint.loginSocket) {
          const {status, result} = await desktopLogin.status(endpoint, request.signal);
          return Response.json(result, {status, headers});
        }
      }
      const response = await forward(request, target, endpoint);
      if (refreshesEndpoint && (response.status === 401 || response.status === 502)) {
        resetEndpoint(pending);
        await response.body?.cancel();
        return failure(503, "unavailable");
      }
      return response;
    } catch {
      if (admin && (target.pathname === "/api/access" || opensAdmin)) admin.show(false);
      if (refreshesEndpoint) resetEndpoint(pending);
      return failure(503, "unavailable");
    }
  };
}

module.exports = { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint, readAdminAccess, requestAdminSession, requestPrivateLogin };
