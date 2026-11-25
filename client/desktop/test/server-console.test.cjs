const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { setImmediate: nextTurn } = require("node:timers/promises");
const test = require("node:test");
const {
  serverConsoleUrl, allowsServerConsoleRequest, createServerConsole,
} = require("../server-console.cjs");

const origin = "http://127.0.0.1:18765";
const landing = new URL("/console/", origin);
const dashboard = new URL("/dashboard/", origin);

test("opens the console at the configured Server origin", () => {
  for (const [server, expected] of [
    [origin, landing.href],
    ["http://localhost:18765/", "http://localhost:18765/console/"],
    ["http://[0:0:0:0:0:0:0:1]:8765", "http://[::1]:8765/console/"],
    ["https://teamup.example", "https://teamup.example/console/"],
  ]) {
    assert.equal(serverConsoleUrl(server).href, expected);
  }
  for (const server of [
    undefined,
    "file:///console/",
    "http://teamup.example",
    "http://127.0.0.1.teamup.example",
    "http://127.0.0.1:0",
    "https://name:secret@teamup.example",
    "https://teamup.example/a/..",
    "https://teamup.example/?",
    "https://teamup.example/#",
  ]) {
    assert.throws(() => serverConsoleUrl(server), /Server origin/);
  }
});

test("opens the dashboard at the configured Server origin", () => {
  assert.equal(serverConsoleUrl(origin, "dashboard").href, dashboard.href);
  assert.throws(() => serverConsoleUrl("http://teamup.example", "dashboard"), /Server origin/);
});

test("allows the console page, assets, operator login, and console reads", () => {
  for (const [path, method, resourceType] of [
    ["/console/", "GET", "mainFrame"],
    ["/console/console.css", "GET", "stylesheet"],
    ["/console/console.js", "GET", "script"],
    ["/v1/auth/login", "POST", "xhr"],
    ["/v1/console/rooms", "GET", "xhr"],
    ["/v1/console/rooms/room/timeline?after=1", "GET", "xhr"],
  ]) {
    assert.ok(allowsServerConsoleRequest(landing, {
      url: origin + path, method, resourceType, initiatorOrigin: origin,
    }), path);
  }
});

test("allows the shared maintenance script and same-origin maintenance reads", () => {
  for (const view of [landing, dashboard]) {
    for (const [url, method, resourceType] of [
      [view.href, "GET", "mainFrame"],
      [origin + "/console/maintenance.js", "GET", "script"],
      [origin + "/v1/auth/login", "POST", "xhr"],
      [origin + "/v1/console/maintenance/health", "GET", "xhr"],
      [origin + "/v1/console/rooms/room/load", "GET", "xhr"],
      [origin + "/v1/console/maintenance/logs?limit=10", "GET", "xhr"],
    ]) {
      assert.ok(allowsServerConsoleRequest(view, { url, method, resourceType, initiatorOrigin: origin }), url);
    }
  }
});

test("blocks navigation and requests outside the operator window's scope", () => {
  for (const [url, method, resourceType, initiatorOrigin = origin] of [
    ["https://teamup.example/console/", "GET", "mainFrame"],
    [dashboard.href, "GET", "mainFrame"],
    [origin + "/console/", "GET", "subFrame"],
    [origin + "/v1/console/rooms", "GET", "mainFrame"],
    [origin + "/v1/auth/login", "GET", "xhr"],
    [origin + "/v1/auth/login?unrelated=1", "POST", "xhr"],
    [origin + "/v1/auth/login", "POST", "xhr", "null"],
    [origin + "/v1/console/rooms", "POST", "xhr"],
    [origin + "/v1/members", "POST", "xhr"],
    ["http://127.0.0.1:8765/v1/auth/login", "POST", "xhr"],
    ["teamup://console/api/workspace", "GET", "xhr"],
  ]) {
    assert.equal(allowsServerConsoleRequest(landing, {
      url, method, resourceType, initiatorOrigin,
    }), false, url);
  }
});

function windowHarness(load) {
  const windows = [];
  const errors = [];
  let headersReceived;
  class OperatorWindow extends EventEmitter {
    constructor() {
      super();
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      this.destroyed = false;
      windows.push(this);
    }

    loadURL(url) {
      this.url = url;
      return load(this, (response) => headersReceived(response, () => {}));
    }

    show() { this.shown = true; }
    isDestroyed() { return this.destroyed; }
    destroy() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const session = new EventEmitter();
  session.setPermissionCheckHandler = () => {};
  session.setPermissionRequestHandler = () => {};
  session.webRequest = {
    onBeforeRequest() {},
    onHeadersReceived(listener) { headersReceived = listener; },
  };
  return {
    windows,
    errors,
    options: {
      BrowserWindow: OperatorWindow,
      dialog: { showErrorBox(_title, message) { errors.push(message); } },
      serverSession: session,
      readOrigin: async () => origin,
    },
  };
}

function servedAssets(paths) {
  return async (window, receiveHeaders) => {
    window.webContents.emit("did-navigate", {}, window.url, 200);
    for (const path of paths) {
      receiveHeaders({ url: new URL(path, window.url).href, statusCode: 200 });
    }
  };
}

test("opens the Room Console on a Server that publishes only the original assets", async () => {
  const { windows, errors, options } = windowHarness(servedAssets([
    "/console/console.css", "/console/console.js",
  ]));
  createServerConsole(options)();
  await nextTurn();
  assert.equal(windows[0].url, landing.href);
  assert.equal(windows[0].shown, true);
  assert.deepEqual(errors, []);
});

test("retries the Server Dashboard after a missing maintenance script becomes available", async () => {
  const deployed = ["/console/console.css", "/console/console.js"];
  const { windows, errors, options } = windowHarness(servedAssets(deployed));
  const show = createServerConsole({ ...options, view: "dashboard" });

  show();
  await nextTurn();
  assert.equal(windows[0].url, dashboard.href);
  assert.equal(windows[0].destroyed, true);
  assert.match(errors[0], /assets/);

  deployed.push("/console/maintenance.js");
  show();
  await nextTurn();
  assert.equal(windows[1].url, dashboard.href);
  assert.equal(windows[1].shown, true);
  assert.equal(errors.length, 1);
});

test("closes a stalled window and makes the Server menu usable again", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { windows, errors, options } = windowHarness(() => new Promise(() => {}));
  const show = createServerConsole(options);

  show();
  await nextTurn();
  assert.equal(windows.length, 1);
  t.mock.timers.runAll();
  await nextTurn();
  assert.equal(windows[0].destroyed, true);
  assert.match(errors[0], /timed out/);
  show();
  await nextTurn();
  assert.equal(windows.length, 2);
});
