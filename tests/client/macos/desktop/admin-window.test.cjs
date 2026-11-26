const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const { allowsAdminRequest, createAdminWindow } = require("../../../../client/macos/desktop/admin-window.cjs");

const server = "http://127.0.0.1:18765";
const landing = new URL(server + "/console/");
const ci = "https://jianyu010.github.io";
const ciPage = ci + "/TeamUp/";
const allows = (url, resourceType, {method = "GET", initiatorOrigin = server} = {}) =>
  allowsAdminRequest(landing, {url, resourceType, initiatorOrigin, method});

test("the Admin renderer can use the Server console and its published account operations", () => {
  assert.equal(allows(landing.href, "mainFrame"), true);
  assert.equal(allows(server + "/console/accounts.js", "script"), true);
  assert.equal(allows(server + "/v1/auth/login", "xhr", {method: "POST"}), true);
  assert.equal(allows(server + "/v1/console/accounts", "xhr", {method: "POST"}), true);
  assert.equal(allows(server + "/v1/console/accounts/new-user/password", "xhr", {method: "POST"}), true);
  assert.equal(allows(server + "/v1/console/accounts/new-user/disable", "xhr", {method: "POST"}), true);
  assert.equal(allows(server + "/v1/console/rooms/a46eb709-3df1-4c7b-9640-34f28c26615d/timeline?after=25", "xhr"), true);
  assert.equal(allows(server + "/v1/console/accounts", "xhr", {initiatorOrigin: ci}), false);
  assert.equal(allows(server + "/v1/console/accounts", "xhr", {method: "DELETE"}), false);
  assert.equal(allows(server + "/v1/members", "xhr", {method: "POST"}), false);
  assert.equal(allows(server + "/v1/console/rooms", "mainFrame"), false);
});

test("CI is confined to its published frame, resources, and read requests", () => {
  assert.equal(allows(ciPage, "subFrame"), true);
  assert.equal(allows(ciPage + "ci.html", "subFrame", {initiatorOrigin: ci}), true);
  assert.equal(allows(ciPage + "assets/app.js", "script", {initiatorOrigin: ci}), true);
  assert.equal(allows(ciPage + "data/ci.jsonl", "xhr", {initiatorOrigin: ci}), true);
  assert.equal(allows(ciPage + "data/ci.jsonl", "xhr"), false);
  assert.equal(allows(ciPage + "?extra=value", "subFrame"), false);
  assert.equal(allows(ciPage + "ci.html", "subFrame", {method: "POST", initiatorOrigin: ci}), false);
  assert.equal(allows(ciPage, "mainFrame"), false);
  assert.equal(allows("https://example.invalid/", "subFrame", {initiatorOrigin: ci}), false);
});

test("Admin opens a separate sandboxed window and reuses it for the same Server", async () => {
  const instances = [];
  let popupHandler;
  class Window extends EventEmitter {
    webContents = new EventEmitter();
    minimized = false;
    showed = 0;
    focused = 0;

    constructor(options) {
      super();
      this.options = options;
      instances.push(this);
      this.webContents.setWindowOpenHandler = (handler) => { popupHandler = handler; };
    }

    async loadURL(url) {
      this.url = url;
      this.webContents.emit("did-navigate", {}, url, 200);
    }

    isMinimized() { return this.minimized; }
    restore() { this.minimized = false; }
    show() { this.showed += 1; }
    focus() { this.focused += 1; }
    isDestroyed() { return this.destroyed === true; }
    destroy() { this.destroyed = true; this.emit("closed"); }
  }
  const adminSession = Object.assign(new EventEmitter(), {
    webRequest: {onBeforeRequest() {}},
    setPermissionCheckHandler(handler) { assert.equal(handler(), false); },
    setPermissionRequestHandler(handler) { handler(null, "test", (allowed) => assert.equal(allowed, false)); },
  });

  const open = createAdminWindow(Window, adminSession);
  await Promise.all([open(landing.href), open(landing.href)]);
  const [admin] = instances;
  assert.equal(instances.length, 1);
  assert.equal(admin.url, landing.href);
  assert.equal(admin.showed, 1);
  assert.equal(admin.options.webPreferences.session, adminSession);
  assert.equal(admin.options.webPreferences.sandbox, true);
  assert.equal(admin.options.webPreferences.contextIsolation, true);
  assert.equal(admin.options.webPreferences.nodeIntegration, false);
  assert.deepEqual(popupHandler(), {action: "deny"});

  let prevented = false;
  admin.webContents.emit("will-frame-navigate", {
    url: ciPage, isMainFrame: true, preventDefault() { prevented = true; },
  });
  assert.equal(prevented, true);
  admin.minimized = true;
  await open(landing.href);
  assert.equal(instances.length, 1);
  assert.equal(admin.minimized, false);
  assert.equal(admin.focused, 1);
  await open("https://teamup.example/console/");
  assert.equal(admin.isDestroyed(), true);
  assert.equal(instances.length, 2);
});
