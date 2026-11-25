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
  assert.match(await response.text(), /<title>TeamUp/);
  assert.match(response.headers.get("Content-Security-Policy"), /connect-src 'self'/);
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

test("reacquires the console endpoint after a stale launch credential", async (t) => {
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

  assert.equal((await handle(consoleRequest("/api/workspace"))).status, 503);
  assert.equal((await handle(consoleRequest("/api/workspace"))).status, 200);
});
