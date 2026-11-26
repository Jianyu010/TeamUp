import assert from "node:assert/strict";
import test from "node:test";
import {sessionPage} from "../../../../client/shared/src/teamup_client/console/console-sessions.mjs";

const session = (id, state = "ready") => ({id, label: id, harness: "codex", state});

test("the default list keeps registered sessions and hides hundreds of MCP placeholders", () => {
  const workspace = {rooms: [], sessions: [
    ...Array.from({length: 262}, (_, i) => session("mcp-" + i, "not_registered")),
    session("Primary"), session("Review"), session("Deploy"),
    {...session("Retry", "unavailable"), member_id: "member"},
    session("Unknown", "unavailable"),
  ]};
  const before = structuredClone(workspace);
  const view = sessionPage(workspace);
  assert.deepEqual(view.sessions.map(({id}) => id), ["Deploy", "Primary", "Retry", "Review"]);
  assert.equal(view.count, 4);
  assert.equal(view.total, 267);
  assert.equal(view.pages, 1);
  assert.deepEqual(workspace, before);
});

test("active tasks and joined rooms take priority with stable label ordering", () => {
  const workspace = {sessions: [session("A"), session("Z"), session("C"), session("B")],
    rooms: [{memberships: [{session: "Z", active_task: true}, {session: "C"}, {session: "B"}]}]};
  assert.deepEqual(sessionPage(workspace).sessions.map(({id}) => id), ["Z", "B", "C", "A"]);
});

test("all sessions are paged in groups of eight and shrinking inventories clamp the page", () => {
  const workspace = {rooms: [], sessions: Array.from({length: 19}, (_, i) =>
    session("mcp-" + i, "not_registered"))};
  const seen = [];
  for (let page = 0; page < 3; page++) {
    const view = sessionPage(workspace, {all: true, page});
    assert.ok(view.sessions.length <= 8);
    assert.equal(view.pages, 3);
    seen.push(...view.sessions.map(({id}) => id));
  }
  assert.equal(new Set(seen).size, 19);
  assert.equal(sessionPage(workspace, {all: true, page: 99}).page, 2);
  workspace.sessions.length = 3;
  assert.equal(sessionPage(workspace, {all: true, page: 2}).page, 0);
  const empty = sessionPage(workspace);
  assert.equal(empty.count, 0);
  assert.equal(empty.start, 0);
  assert.equal(empty.page, 0);
  assert.equal(empty.total, 3);
});

test("search matches labels, harnesses and identifiers within the chosen scope", () => {
  const workspace = {rooms: [], sessions: [
    {...session("id-1"), label: "Deployment"},
    {...session("id-2"), harness: "Claude Code"}, session("mcp-3", "not_registered"),
  ]};
  const ids = (options) => sessionPage(workspace, options).sessions.map(({id}) => id);
  assert.deepEqual(ids({query: " deploy "}), ["id-1"]);
  assert.deepEqual(ids({query: "CLAUDE"}), ["id-2"]);
  assert.deepEqual(ids({query: "ID-2"}), ["id-2"]);
  assert.deepEqual(ids({query: "mcp"}), []);
  assert.deepEqual(ids({all: true, query: "MCP"}), ["mcp-3"]);
  assert.deepEqual(ids({all: true, query: "missing"}), []);
});

test("refresh keeps the selected session visible when its page changes", () => {
  const workspace = {rooms: [], sessions: Array.from({length: 17}, (_, i) => session("Session " + i))};
  const view = sessionPage(workspace, {page: 0, focused: "Session 16"});
  assert.equal(view.page, 2);
  assert.equal(view.sessions[0].id, "Session 16");
  workspace.sessions.unshift(session("Earlier"));
  const updated = sessionPage(workspace, {page: view.page, focused: "Session 16"});
  assert.ok(updated.sessions.some(({id}) => id === "Session 16"));
  assert.equal(sessionPage(workspace, {page: 2, query: "Earlier", focused: "Session 16"}).page, 0);
});
