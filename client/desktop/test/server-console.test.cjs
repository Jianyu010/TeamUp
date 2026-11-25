const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { setImmediate: nextTurn } = require("node:timers/promises");
const test = require("node:test");
const {
  serverConsoleUrl, allowsServerConsoleRequest, createServerConsole,
} = require("../server-console.cjs");

const origin = "http://127.0.0.1:18765";
const landing = new URL("/console/", origin);

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

test("blocks navigation and requests outside the operator window's scope", () => {
  for (const [url, method, resourceType, initiatorOrigin = origin] of [
    ["https://teamup.example/console/", "GET", "mainFrame"],
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

test("closes a stalled window and makes the Server menu usable again", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const windows = [];
  const errors = [];
  class StalledWindow extends EventEmitter {
    constructor() {
      super();
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      this.destroyed = false;
      windows.push(this);
    }

    loadURL() { return new Promise(() => {}); }
    isDestroyed() { return this.destroyed; }
    destroy() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const session = new EventEmitter();
  session.setPermissionCheckHandler = () => {};
  session.setPermissionRequestHandler = () => {};
  session.webRequest = { onBeforeRequest() {}, onHeadersReceived() {} };
  const show = createServerConsole({
    BrowserWindow: StalledWindow,
    dialog: { showErrorBox(_title, message) { errors.push(message); } },
    serverSession: session,
    readOrigin: async () => origin,
  });

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
