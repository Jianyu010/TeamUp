const assert = require("node:assert/strict");
const { once } = require("node:events");
const http = require("node:http");
const { resolve } = require("node:path");
const test = require("node:test");
const { CONSOLE_URL, createConsoleHandler, parseConsoleEndpoint } = require("../console-protocol.cjs");

const assets = resolve(__dirname, "../../src/teamup_client/console");
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

test("relays named reads and task actions with the main process credential", async (t) => {
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
