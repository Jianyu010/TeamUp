const { isIPv4 } = require("node:net");
const { createRemoteWindow } = require("./remote-window.cjs");

const roomAssets = new Map([
  ["/console/console.css", "stylesheet"],
  ["/console/console.js", "script"],
]);
const assets = new Map([...roomAssets, ["/console/maintenance.js", "script"]]);
const views = {
  rooms: {
    path: "/console/",
    title: "Room Console",
    requiredAssets: Array.from(roomAssets.keys()),
  },
  dashboard: {
    path: "/dashboard/",
    title: "Server Dashboard",
    requiredAssets: Array.from(assets.keys()),
  },
};

function serverConsoleUrl(value, view = "rooms") {
  const origin = typeof value === "string" &&
    /^https?:\/\/[^/?#\\@\s\x00-\x1f\x7f]+\/?$/i.test(value) &&
    URL.parse(value);
  if (origin && origin.port !== "0") {
    const host = origin.hostname;
    const loopback = host === "localhost" || host === "[::1]" ||
      (isIPv4(host) && host.startsWith("127."));
    if (origin.protocol === "https:" || loopback) {
      return new URL(views[view].path, origin);
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

function createServerConsole({ BrowserWindow, dialog, serverSession, readOrigin, view = "rooms" }) {
  const { title, requiredAssets } = views[view];
  return createRemoteWindow({
    BrowserWindow,
    dialog,
    remoteSession: serverSession,
    title,
    retryHint: "Check the configured Server and its connection, then try again.",
    resolveView: async () => {
      const landing = serverConsoleUrl(await readOrigin(), view);
      return {
        landing,
        requiredAssets,
        allowsRequest: (request) => allowsServerConsoleRequest(landing, request),
      };
    },
  });
}

module.exports = { serverConsoleUrl, allowsServerConsoleRequest, createServerConsole };
