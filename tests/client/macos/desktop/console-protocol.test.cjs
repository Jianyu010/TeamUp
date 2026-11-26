const assert = require("node:assert/strict");
const { once } = require("node:events");
const { mkdtemp, rm } = require("node:fs/promises");
const http = require("node:http");
const { join, resolve } = require("node:path");
const test = require("node:test");
const { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint } = require("../../../../client/macos/desktop/console-protocol.cjs");

const assets = resolve(__dirname, "../../../../client/shared/src/teamup_client/console");
const origin = "teamup://console";
const token = "a".repeat(43);

function consoleRequest(path, options, initiator = origin) {
  return Object.assign(new Request(origin + path, options), { initiatorOrigin: initiator });
}

test("serves the bundled console without a running Client", async () => {
  const handle = createConsoleHandler(assets, () => null);
  const response = await handle(new Request(CONSOLE_URL));
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>TeamUp/);
  assert.match(html, /<footer[^>]*>[\s\S]*id="client-version"[\s\S]*id="server-version"[\s\S]*<\/footer>/);
  assert.match(response.headers.get("Content-Security-Policy"), /connect-src 'self'/);
  assert.match(html, /id="board-toggle"[^>]*aria-controls="board-panel"[^>]*aria-expanded="false"/);
  assert.match(html, /id="room-id"[\s\S]*id="board-panel"[^>]*hidden/);
  assert.doesNotMatch(html, /id="room-session-actions"/);
  for (const asset of ["/console-members.js", "/console-history.mjs", "/console-board.js", "/console-sessions.js"]) {
    const script = await handle(new Request(origin + asset));
    assert.equal(script.status, 200);
    assert.equal(script.headers.get("Content-Type"), "text/javascript; charset=utf-8");
  }
  assert.equal((await handle(consoleRequest("/launch.py"))).status, 404);
  assert.equal((await handle(new Request("teamup://elsewhere/"))).status, 404);
});

test("accepts only the Client's loopback launch URL", () => {
  assert.deepEqual(parseConsoleEndpoint(`http://127.0.0.1:1234/#${token}\n`), {
    port: 1234,
    token,
  });
  for (const url of [
    `http://localhost:1234/#${token}`,
    `https://127.0.0.1:1234/#${token}`,
    `http://127.0.0.1:65536/#${token}`,
    `http://127.0.0.1:1234/extra#${token}`,
    "http://127.0.0.1:1234/",
  ]) {
    assert.throws(() => parseConsoleEndpoint(url), /Client console address/);
  }
});

test("relays named reads and writes with the main process credential", async (t) => {
  const received = [];
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push({
      url: request.url,
      method: request.method,
      auth: request.headers.authorization,
      origin: request.headers.origin,
      cookie: request.headers.cookie,
      body: Buffer.concat(chunks).toString(),
    });
    if (request.url === "/api/team?session=redirect") {
      response.writeHead(302, { Location: "/unexpected" });
      response.end();
      return;
    }
    response.writeHead(request.method === "POST" ? 409 : 200, {
      "Content-Type": "application/json; charset=utf-8",
      "Set-Cookie": "unexpected=1",
    });
    response.end(JSON.stringify({ state: request.method === "POST" ? "conflict" : "ready" }));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.close());
  const { port } = server.address();
  const handle = createConsoleHandler(assets, () => ({ port, token }));

  const read = await handle(consoleRequest("/api/team?session=local", {
    headers: { Authorization: "untrusted", Cookie: "untrusted" },
  }));
  assert.equal(read.status, 200);
  assert.deepEqual(await read.json(), { state: "ready" });
  assert.equal(read.headers.get("Set-Cookie"), null);

  const roomMembers = "/api/room-members?room=01234567-89ab-4cde-8123-456789abcdef&session=local";
  const roster = await handle(consoleRequest(roomMembers));
  assert.equal(roster.status, 200);
  assert.deepEqual(await roster.json(), { state: "ready" });
  assert.equal((await handle(consoleRequest(roomMembers, { method: "POST" }))).status, 404);
  assert.equal((await handle(consoleRequest(roomMembers, {}, "https://example.com"))).status, 403);

  const versions = await handle(consoleRequest("/api/versions"));
  assert.equal(versions.status, 200);
  assert.deepEqual(await versions.json(), { state: "ready" });
  assert.equal((await handle(consoleRequest("/api/versions", {}, "https://example.com"))).status, 403);

  const archive = "/api/retired?session=retired";
  const leave = "/api/sessions/leave?session=local&room=room";
  const retire = "/api/sessions/retire?session=local";
  assert.equal((await handle(consoleRequest(archive))).status, 200);
  for (const action of [leave, retire]) {
    assert.equal((await handle(consoleRequest(action, { method: "POST" }))).status, 409);
    assert.equal((await handle(consoleRequest(action))).status, 404);
    assert.equal((await handle(consoleRequest(action, { method: "POST" }, "https://example.org"))).status, 403);
  }

  const board = "/api/room-board?room=01234567-89ab-4cde-8123-456789abcdef&session=local";
  const boardBody = JSON.stringify({ version: 1, rules: "Rules", protocol: "Protocol" });
  assert.equal((await handle(consoleRequest(board))).status, 200);
  const publish = { method: "POST", headers: { "Content-Type": "application/json" }, body: boardBody };
  const conflict = await handle(consoleRequest(board, publish));
  assert.equal(conflict.status, 409);
  assert.deepEqual(await conflict.json(), { state: "conflict" });
  assert.equal((await handle(consoleRequest(board, publish, "https://example.com"))).status, 403);
  assert.equal((await handle(consoleRequest(board, { method: "DELETE" }))).status, 404);

  const remove = "/api/room-members/remove?room=room&session=local&member=target";
  const membership = JSON.stringify({ membership_id: "a".repeat(32) });
  const removeAction = { ...publish, body: membership };
  assert.equal((await handle(consoleRequest(remove, removeAction))).status, 409);
  assert.equal((await handle(consoleRequest(remove, removeAction, "https://example.com"))).status, 403);
  assert.equal((await handle(consoleRequest(remove))).status, 404);

  const task = "/api/tasks/01234567-89ab-4cde-8123-456789abcdef/progress?room=room&session=local";
  const body = JSON.stringify({ client_progress_id: "progress-1", note: "In review" });
  const write = await handle(consoleRequest(task, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  }));
  assert.equal(write.status, 409);
  assert.deepEqual(await write.json(), { state: "conflict" });

  assert.equal((await handle(consoleRequest("/api/workspace", {}, "https://example.com"))).status, 403);
  assert.equal((await handle(new Request(origin + "/api/workspace"))).status, 403);
  assert.equal((await handle(consoleRequest("/api/unknown"))).status, 404);
  const redirect = await handle(consoleRequest("/api/team?session=redirect"));
  assert.equal(redirect.status, 502);
  assert.equal(redirect.headers.get("Location"), null);

  assert.deepEqual(received, [
    ["/api/team?session=local", "GET", ""],
    [roomMembers, "GET", ""],
    ["/api/versions", "GET", ""],
    [archive, "GET", ""],
    [leave, "POST", ""],
    [retire, "POST", ""],
    [board, "GET", ""],
    [board, "POST", boardBody],
    [remove, "POST", membership],
    [task, "POST", body],
    ["/api/team?session=redirect", "GET", ""],
  ].map(([url, method, body]) => ({
    url,
    method,
    auth: `Bearer ${token}`,
    origin: `http://127.0.0.1:${port}`,
    cookie: undefined,
    body,
  })));
});

test("relays sign-in from the local console to the private socket only", async (t) => {
  let calls = 0;
  const directory = await mkdtemp("/tmp/teamup-console-");
  const loginSocket = join(directory, "login.sock");
  const server = http.createServer((request, response) => {
    assert.equal(request.url, "/api/login");
    assert.equal(request.method, "GET");
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({state: "sign_in_required"}));
  });
  const login = http.createServer(async (request, response) => {
    calls += 1;
    assert.equal(request.url, "/api/login");
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    assert.equal(request.headers.origin, `http://127.0.0.1:${server.address().port}`);
    response.setHeader("Content-Type", "application/json");
    assert.equal(request.method, "POST");
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    assert.equal(Buffer.concat(chunks).toString(), body);
    response.writeHead(401);
    response.end(JSON.stringify({state: "wrong_credentials"}));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  login.listen(loginSocket);
  await once(login, "listening");
  t.after(async () => {
    server.close();
    login.close();
    await rm(directory, {recursive: true});
  });
  const handle = createConsoleHandler(assets, () => ({port: server.address().port, token, loginSocket}));
  const body = JSON.stringify({username: "alice", password: "example password"});
  const options = {method: "POST", headers: {"Content-Type": "application/json"}, body};

  assert.equal((await handle(consoleRequest("/api/login", options, "https://example.com"))).status, 403);
  assert.deepEqual(await (await handle(consoleRequest("/api/login"))).json(), {state: "sign_in_required"});
  const rejected = await handle(consoleRequest("/api/login", options));
  assert.equal(rejected.status, 401);
  assert.deepEqual(await rejected.json(), {state: "wrong_credentials"});
  assert.equal(calls, 1);
});

for (const route of ["/api/workspace", "/api/versions"]) {
  test(`reacquires the console endpoint for ${route} after a stale launch credential`, async (t) => {
    let accepted = false;
    const server = http.createServer((_request, response) => {
      response.writeHead(accepted ? 200 : 401, { "Content-Type": "application/json" });
      response.end("{}");
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    t.after(() => server.close());
    const first = Promise.resolve({ port: server.address().port, token });
    let endpoint = first;
    const handle = createConsoleHandler(assets, () => endpoint, (stale) => {
      assert.equal(stale, first);
      accepted = true;
      endpoint = Promise.resolve({ port: server.address().port, token: "b".repeat(43) });
    });

    assert.equal((await handle(consoleRequest(route))).status, 503);
    assert.equal((await handle(consoleRequest(route))).status, 200);
  });
}

test("remembered sign-in stays on the desktop side of the bridge", async (t) => {
  const nativeLogin = require("../../../../client/macos/desktop/console-login.cjs");
  const profile = "/private/test/teamup.json";
  let remember_scope = "b".repeat(64);
  const credentials = {username: "alice", password: "desktop test password"};
  const writes = [];
  let saved = null;
  const store = {
    get: async (path) => {
      assert.equal(path, profile);
      return saved;
    },
    put: async (path, value) => {
      assert.equal(path, profile);
      writes.push("put");
      saved = value;
    },
    forget: async (path) => {
      assert.equal(path, profile);
      writes.push("forget");
      saved = null;
    },
  };
  let state = "sign_in_required";
  let loginStatus = 401;
  const calls = [];
  const directory = await mkdtemp("/tmp/teamup-console-");
  const loginSocket = join(directory, "login.sock");
  const login = http.createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    if (request.method === "GET") {
      response.end(JSON.stringify({state, remember_scope}));
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    calls.push(JSON.parse(Buffer.concat(chunks)));
    response.writeHead(loginStatus);
    if (loginStatus === 200 && state === "sign_in_required") {
      state = "ready";
      response.end(JSON.stringify({state, authenticated_scope: remember_scope}));
    } else {
      response.end(JSON.stringify({state: loginStatus === 401 ? "wrong_credentials" : state}));
    }
  });
  login.listen(loginSocket);
  await once(login, "listening");
  t.after(async () => {
    login.close();
    await rm(directory, {recursive: true});
  });
  const endpoint = {port: 1234, token, loginSocket};
  const manager = nativeLogin.createDesktopLogin(store, profile);
  const handle = createConsoleHandler(assets, () => endpoint, undefined, manager);
  const submit = (remember, initiator) => handle(consoleRequest("/api/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({...credentials, remember}),
  }, initiator));

  assert.equal((await submit(true, "https://untrusted.example")).status, 403);
  assert.equal((await submit(true)).status, 401);
  assert.deepEqual(writes, []);
  loginStatus = 200;
  const accepted = await submit(true);
  assert.equal(accepted.status, 200);
  assert.deepEqual(await accepted.json(), {state: "ready"});
  assert.deepEqual(saved, {...credentials, scope: remember_scope});
  assert.deepEqual(calls, [credentials, credentials]);
  await submit(false); // An active Client did not authenticate this submission.
  assert.deepEqual(writes, ["put"]);

  state = "sign_in_required";
  saved = {...credentials, scope: "c".repeat(64)};
  const attemptCount = calls.length;
  await manager.restore(endpoint);
  assert.equal(calls.length, attemptCount);
  saved = {...credentials, scope: remember_scope};
  const scope = remember_scope;
  remember_scope = null;
  await manager.restore(endpoint);
  assert.equal(calls.length, attemptCount);
  const unavailable = await handle(consoleRequest("/api/login"));
  assert.deepEqual(await unavailable.json(), {state, remember_available: false});
  remember_scope = scope;
  const pending = await handle(consoleRequest("/api/login"));
  assert.deepEqual(await pending.json(), {state, remember_available: true});
  await manager.restore(endpoint);
  assert.deepEqual(calls.at(-1), {...credentials, remember_scope});
  assert.equal(state, "ready");

  state = "sign_in_required";
  loginStatus = 401;
  await manager.restore(endpoint);
  assert.equal(saved, null);

  loginStatus = 200;
  await submit(true);
  await manager.forget();
  assert.equal(saved, null);

  state = "sign_in_required";
  saved = {...credentials, scope};
  remember_scope = null;
  const manual = await submit(false);
  assert.deepEqual(await manual.json(), {state: "ready"});
  assert.equal(saved, null);
});

test("opens only the current operator's configured console and forwards no renderer URL", async (t) => {
  let destination = "https://teamup.example/console/";
  let launchFails = false;
  const server = http.createServer((request, response) => {
    assert.equal(request.url, "/api/access");
    assert.equal(request.method, "GET");
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    response.writeHead(200, {"Content-Type": "application/json"});
    response.end(JSON.stringify(destination ? {admin_url: destination} : {}));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.close());
  const opened = [];
  const visibility = [];
  const handle = createConsoleHandler(assets, () => ({port: server.address().port, token}),
    undefined, undefined, {
      show: (visible) => visibility.push(visible),
      open: async ({url}) => {
        if (launchFails) throw new Error("Server Console did not load");
        opened.push(url);
      },
    });
  const open = {method: "POST"};
  assert.equal((await handle(consoleRequest("/api/admin/session", open))).status, 404);
  assert.equal((await handle(consoleRequest("/api/open-admin", open, "https://example.com"))).status, 403);
  assert.equal((await handle(consoleRequest("/api/open-admin", {...open, body: "{}"}))).status, 400);
  assert.deepEqual(await (await handle(consoleRequest("/api/access"))).json(), {admin_url: destination});
  assert.deepEqual(await (await handle(consoleRequest("/api/open-admin", open))).json(), {state: "opened"});
  assert.deepEqual(opened, [destination]);
  launchFails = true;
  assert.equal((await handle(consoleRequest("/api/open-admin", open))).status, 503);
  assert.equal(visibility.at(-1), true);
  destination = "http://not-loopback.example/console/";
  assert.equal((await handle(consoleRequest("/api/open-admin", open))).status, 403);
  destination = undefined;
  assert.deepEqual(await (await handle(consoleRequest("/api/access"))).json(), {});
  assert.deepEqual(visibility, [true, true, true, false, false]);
});
