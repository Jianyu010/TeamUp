import assert from "node:assert/strict";
import test from "node:test";
import {leadershipDialog} from "../../../../../../client/shared/src/teamup_client/console/console-focused-rooms.mjs";

const room = {id: "room", name: "Release"};
const leader = {id: "leader-session", label: "Release lead"};
const candidates = [
  {member_id: "a", membership_id: "first-join", role_description: "First", reason: "leadership_terms"},
  {member_id: "b", membership_id: "second-join", role_description: "Second", reason: "recent_work"},
];

class Element {
  children = [];
  listeners = new Map();
  value = "";
  open = false;
  disabled = false;
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  setAttribute() {}
  replaceChildren(...children) { this.children = children; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.get("close")?.(); }
  click() {
    assert.equal(this.disabled, false);
    return this.listeners.get("click")();
  }
}

function dialog(handoff, list = {state: "ready", leadership_epoch: 4, candidates}) {
  const elements = new Map();
  const requests = [];
  const departures = [];
  let refreshes = 0;
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const ready = leadershipDialog({
    get,
    node: (_tag, label) => Object.assign(new Element(), {textContent: label}),
    request: async (path, params, action) => {
      requests.push({path, params, action});
      if (action) return handoff;
      if (typeof list === "function") return list();
      return list;
    },
    departed: (...args) => { departures.push(args); },
    refresh: async () => { refreshes++; },
  });
  return {
    ...ready, get, requests, departures, refreshes: () => refreshes,
    open: () => ready.open(leader, room, "leader-join"),
    writes: () => requests.filter(({action}) => action).map(({action}) => action.body),
  };
}

test("opening and dismissing leader Leave never writes", async () => {
  const controls = dialog(null);
  await controls.open();
  assert.equal(controls.get("leadership-dialog").open, true);
  assert.deepEqual(controls.get("leadership-candidate").children.map(({textContent}) => textContent),
    ["Choose a successor", "1. First", "2. Second"]);
  controls.get("leadership-dismiss").click();
  assert.equal(controls.get("leadership-dialog").open, false);
  assert.deepEqual(controls.writes(), []);
  assert.deepEqual(controls.requests.map(({path, params}) => [path, params]), [
    ["/api/room-leadership", {room: room.id, session: leader.id}],
  ]);
});

test("choosing a successor submits the selected membership and candidate epoch", async () => {
  const receipt = {state: "ready", pending: false, left: true};
  const controls = dialog(receipt);
  await controls.open();
  const choice = controls.get("leadership-candidate");
  choice.value = "b";
  choice.listeners.get("change")();
  await controls.get("leadership-choose").click();
  assert.deepEqual(controls.writes(), [{
    leadership_epoch: 4, mode: "choose", leave: true, member_id: "b", membership_id: "second-join",
  }]);
  assert.deepEqual(controls.departures, [[leader, room, receipt, "leader-join"]]);
});

test("automatic selection uses ranking even when the candidate list is empty", async () => {
  const controls = dialog({state: "ready", pending: false, left: true},
    {state: "ready", leadership_epoch: 4, candidates: []});
  await controls.open();
  assert.equal(controls.get("leadership-choose").disabled, true);
  await controls.get("leadership-auto").click();
  assert.deepEqual(controls.writes(), [{leadership_epoch: 4, mode: "auto", leave: true}]);
});

test("deferring keeps the membership visible and refreshes the server outcome", async () => {
  const controls = dialog({state: "ready", pending: true, left: false});
  await controls.open();
  await controls.get("leadership-defer").click();
  assert.deepEqual(controls.writes(), [{leadership_epoch: 4, mode: "defer", leave: true}]);
  assert.deepEqual(controls.departures, []);
  assert.equal(controls.refreshes(), 1);
  assert.match(controls.get("session-action-status").textContent, /still in the room/);
});

test("a stale successor forces a fresh read and clears the previous selection", async () => {
  let reads = 0;
  const controls = dialog({state: "conflict"}, () => ({
    state: "ready", leadership_epoch: 4 + reads++, candidates,
  }));
  await controls.open();
  const choice = controls.get("leadership-candidate");
  choice.value = "a";
  choice.listeners.get("change")();
  await controls.get("leadership-choose").click();
  assert.equal(controls.get("leadership-dialog").open, true);
  assert.equal(controls.get("leadership-choose").disabled, true);
  assert.match(controls.get("leadership-status").textContent, /changed/);
  assert.equal(reads, 2);
  assert.deepEqual(controls.departures, []);
});
