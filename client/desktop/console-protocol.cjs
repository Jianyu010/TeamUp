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
  ["/console-sessions.mjs", ["console-sessions.mjs", "text/javascript; charset=utf-8"]],
  ["/console-members.js", ["console-members.js", "text/javascript; charset=utf-8"]],
  ["/console-board.js", ["console-board.js", "text/javascript; charset=utf-8"]],
  ["/console-tasks.js", ["console-tasks.js", "text/javascript; charset=utf-8"]],
  ["/console-sessions.js", ["console-sessions.js", "text/javascript; charset=utf-8"]],
  ["/jet-pair.png", ["jet-pair.png", "image/png"]],
]);
const reads = new Set([
  "/api/workspace", "/api/versions", "/api/retired", "/api/team", "/api/room", "/api/room-members",
  "/api/room-board", "/api/tasks", "/api/task-events",
]);
const taskAction = /^\/api\/tasks\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\/(?:join|progress|finish)$/i;
const actions = new Set([
  "/api/sessions/leave", "/api/sessions/retire", "/api/room-board", "/api/room-members/remove",
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
  const match = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/#([A-Za-z0-9_-]{43})$/.exec(value.trim());
  const port = Number(match?.[1]);
  if (!match || port < 1 || port > 65535) {
    throw new Error("TeamUp did not receive a valid Client console address.");
  }
  return { port, token: match[2] };
}

async function forward(request, target, { port, token }) {
  const clientHeaders = {
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
      hostname: "127.0.0.1",
      port,
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

function createConsoleHandler(directory, getEndpoint, resetEndpoint = () => {}) {
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
    const isRead = request.method === "GET" && reads.has(target.pathname);
    const isAction = request.method === "POST" &&
      (actions.has(target.pathname) || taskAction.test(target.pathname));
    if (!isRead && !isAction) return failure(404, "not_found");
    if (request.initiatorOrigin !== CONSOLE_ORIGIN) return failure(403, "forbidden");

    const pending = getEndpoint();
    const refreshesEndpoint = target.pathname === "/api/workspace" || target.pathname === "/api/versions";
    try {
      const endpoint = await pending;
      if (!endpoint) return failure(503, "unavailable");
      const response = await forward(request, target, endpoint);
      if (refreshesEndpoint && (response.status === 401 || response.status === 502)) {
        resetEndpoint(pending);
        await response.body?.cancel();
        return failure(503, "unavailable");
      }
      return response;
    } catch {
      if (refreshesEndpoint) resetEndpoint(pending);
      return failure(503, "unavailable");
    }
  };
}

module.exports = { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint };
