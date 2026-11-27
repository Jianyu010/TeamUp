import assert from "node:assert/strict";
import test from "node:test";
import {
  focusedRoomPanel, isRoomLeader, leadershipDialog, leadershipKnown, leaseWindow,
} from "../../../../../../client/shared/src/teamup_client/console/console-focused-rooms.mjs";

const room = {id: "room", name: "Release"};
const leader = {id: "leader-session", label: "Release lead", member_id: "lead"};
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

test("selecting a candidate shows their account, responsibility and latest work", async () => {
  const candidate = {
    ...candidates[0], reason: "leader_profile", owner_username: "alice",
    description: "Verifies releases.", recent_work: {
      kind: "task", task_number: 4, title: "Check smoke results", event: "finished",
      created_at: "2026-11-28T03:00:00Z",
    },
  };
  const controls = dialog(null, {state: "ready", leadership_epoch: 4, candidates: [candidate]});
  await controls.open();
  const choice = controls.get("leadership-candidate");
  choice.value = candidate.member_id;
  choice.listeners.get("change")();
  const detail = controls.get("leadership-detail").textContent;
  assert.match(detail, /Leadership role/);
  assert.match(detail, /alice/);
  assert.match(detail, /Verifies releases\./);
  assert.match(detail, /Task #4 Check smoke results finished/);
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

test("a pending decision ends when a refreshed workspace no longer lists the leader", async () => {
  const controls = dialog({state: "ready", pending: true, left: false});
  await controls.open();
  assert.equal(controls.get("leadership-session").textContent, leader.label);
  assert.equal(controls.get("leadership-room-name").textContent, room.name);
  await controls.get("leadership-defer").click();
  const joined = {sessions: [{id: leader.id, state: "ready"}], rooms: [{id: room.id, memberships: [{session: leader.id}]}]};
  controls.observe(joined);
  controls.observe({sessions: [], rooms: []});
  assert.match(controls.get("session-action-status").textContent, /still in the room/);
  controls.observe({...joined, rooms: [{id: room.id, memberships: []}]});
  assert.equal(controls.get("session-action-status").textContent, "Release lead is no longer in Release.");
  controls.observe(joined);
  assert.equal(controls.get("session-action-status").textContent, "Release lead is no longer in Release.");

  const unavailable = dialog({state: "ready", pending: true, left: false});
  await unavailable.open();
  await unavailable.get("leadership-defer").click();
  unavailable.observe({...joined, sessions: [{id: leader.id, state: "not_registered"}]});
  assert.match(unavailable.get("session-action-status").textContent, /still in the room/);
  unavailable.observe({sessions: [], rooms: joined.rooms});
  assert.equal(unavailable.get("session-action-status").textContent,
    "Release lead is no longer available. Check its membership in Release.");
});

test("leadership follows the published epoch and lease renewal opens fifteen minutes early", () => {
  const session = {id: "leader-session", member_id: "lead"};
  assert.equal(isRoomLeader({leadership_epoch: 4, leader_member_id: "lead"}, session, {}), true);
  assert.equal(isRoomLeader({leadership_epoch: 4, leader_member_id: "other"}, session, {can_remove_members: true}), false);
  assert.equal(isRoomLeader({memberships: []}, session, {can_remove_members: true}), true);
  assert.equal(isRoomLeader({memberships: []}, session, {}), false);
  assert.equal(isRoomLeader({leadership_epoch: 4, leader_member_id: "lead"}, null, {}), false);
  assert.equal(isRoomLeader({leadership_epoch: 4, leader_member_id: "lead"}, session, undefined), false);
  assert.equal(leadershipKnown({leadership_epoch: 4}), true);
  assert.equal(leadershipKnown({memberships: [{can_remove_members: true}]}), true);
  assert.equal(leadershipKnown({memberships: [{session: "leader-session"}]}), false);

  const now = Date.parse("2026-11-28T12:00:00Z");
  const at = (offset) => new Date(now + offset).toISOString();
  assert.deepEqual([16 * 60_000, 15 * 60_000, 1, 0, -1].map((offset) => leaseWindow(at(offset), now)),
    ["open", "due", "due", "expired", "expired"]);
  assert.equal(leaseWindow(undefined, now), "unknown");
});

const peer = {id: "peer-session", label: "Release peer", member_id: "peer", state: "ready"};
const root = {
  id: "room", name: "Release", leadership_epoch: 4, leader_member_id: "lead",
  memberships: [{session: leader.id, membership_id: "leader-join", acknowledged: 1, current: 1, can_remove_members: true}],
};
const empty = {state: "ready", rooms: [], next_after: null};
const future = (minutes) => new Date(Date.now() + minutes * 60_000).toISOString();
const past = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

class Control {
  children = [];
  listeners = new Map();
  dataset = {};
  value = "";
  text = "";
  hidden = false;
  disabled = false;

  get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) {
    this.children = [];
    this.text = String(value);
  }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  append(...items) {
    this.children.push(...items.map((item) => typeof item === "string" ? {textContent: item} : item));
  }
  replaceChildren(...children) { this.children = children; }
  contains(item) { return this === item || this.children.some((child) => child.contains?.(item)); }
  focus() { globalThis.document.activeElement = this; }
  click() {
    assert.equal(this.disabled, false);
    return this.listeners.get("click")();
  }
  submit() { return this.listeners.get("submit")({preventDefault() {}}); }
}

function panel(t, {room = root, rooms, sessions = [leader, peer], respond} = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Control());
    return elements.get(id);
  };
  const state = {room, actor: leader};
  const requests = [];
  const events = {changed: 0, opened: []};
  globalThis.document = {activeElement: null};
  t.after(() => { delete globalThis.document; });
  const api = focusedRoomPanel({
    get,
    node: (tag, textContent, className) => Object.assign(new Control(), {tag, text: textContent ?? "", className}),
    request: async (path, params, action) => {
      requests.push({path, params, action});
      return respond ? respond(path, params, action) : empty;
    },
    room: () => state.room,
    actor: () => state.actor,
    rooms: () => rooms ?? [state.room],
    sessions: () => sessions,
    active: () => true,
    openRoom: (id) => events.opened.push(id),
    changed: async () => { events.changed++; },
  });
  return {
    api, get, requests, events, state,
    writes: () => requests.filter(({action}) => action).map(({path, params, action}) => [path, params, action.body]),
    text: (id) => get(id).textContent,
  };
}

test("creating a child room reuses one request ID until the Server answers", async (t) => {
  let answer = {state: "unavailable"};
  const view = panel(t, {respond: (path, params, action) => action ? answer : empty});
  await view.api.refresh();
  view.get("focus-create-name").value = "  Investigation  ";
  view.get("focus-create-rules").value = " Stay on topic ";
  view.get("focus-create-protocol").value = "   ";
  view.get("focus-create-history").value = "retained";
  await view.get("focus-create-form").submit();
  assert.match(view.text("focus-status"), /could not confirm the request\. A retry repeats the same request/);
  assert.equal(view.get("focus-create-name").value, "  Investigation  ");

  answer = {state: "ready", room_id: "child", name: "Investigation", expires_at: future(300)};
  await view.get("focus-create-form").submit();
  const writes = view.writes();
  assert.deepEqual(writes.map(([path, params]) => [path, params]), [
    ["/api/child-rooms", {room: "room", session: "leader-session"}],
    ["/api/child-rooms", {room: "room", session: "leader-session"}],
  ]);
  assert.match(writes[0][2].client_request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(writes[1][2], writes[0][2]);
  assert.deepEqual(Object.keys(writes[0][2]).sort(), ["client_request_id", "history_policy", "name", "rules"]);
  assert.equal(writes[0][2].name, "Investigation");
  assert.equal(writes[0][2].rules, "Stay on topic");
  assert.equal(view.get("focus-create-name").value, "");
  assert.equal(view.get("focus-create-history").value, "since_joining");
  assert.match(view.text("focus-status"), /Investigation was created/);
  assert.equal(view.events.changed, 1);
  assert.equal(view.requests.filter(({path, action}) => path === "/api/child-rooms" && !action).length, 2);
});

test("a stale answer reloads the room and the next attempt uses a fresh request ID", async (t) => {
  let answer = {state: "conflict"};
  const view = panel(t, {respond: (path, params, action) => action ? answer : empty});
  await view.api.refresh();
  view.get("focus-create-name").value = "Investigation";
  await view.get("focus-create-form").submit();
  assert.match(view.text("focus-status"), /changed\. Review the current state and try again/);
  assert.equal(view.events.changed, 1);
  assert.equal(view.requests.filter(({path, action}) => path === "/api/child-rooms" && !action).length, 2);

  answer = {state: "ready", room_id: "child", name: "Investigation", expires_at: future(300)};
  await view.get("focus-create-form").submit();
  const [first, second] = view.writes();
  assert.notEqual(first[2].client_request_id, second[2].client_request_id);
});

test("changing the acting session reads through the new scope and keeps retries per scope", async (t) => {
  const shared = {...root, memberships: [...root.memberships, {session: peer.id, acknowledged: 1, current: 1}]};
  const view = panel(t, {respond: (path, params, action) => action ? {state: "unavailable"} : empty});
  await view.api.refresh();
  view.get("focus-create-name").value = "Investigation";
  await view.get("focus-create-form").submit();
  const [first] = view.writes();
  view.state.actor = peer;
  view.state.room = shared;
  await view.api.refresh();
  view.get("focus-create-name").value = "Investigation";
  await view.get("focus-create-form").submit();
  const second = view.writes()[1];
  assert.equal(second[1].session, "peer-session");
  assert.notEqual(second[2].client_request_id, first[2].client_request_id);

  view.state.actor = leader;
  view.state.room = root;
  await view.api.refresh();
  view.get("focus-create-name").value = "Investigation";
  await view.get("focus-create-form").submit();
  assert.equal(view.writes()[2][2].client_request_id, first[2].client_request_id);

  view.api.reset();
  assert.equal(view.get("focus-children").children.length, 0);
  assert.equal(view.text("focus-status"), "");
});

test("a child room shows its parent and lease instead of child controls", async (t) => {
  const child = {...root, id: "child", name: "Investigation", parent_room_id: "room",
    created_at: past(240), expires_at: future(5)};
  const view = panel(t, {room: child, rooms: [root, child]});
  await view.api.refresh();
  assert.equal(view.get("focus-lease-section").hidden, false);
  assert.equal(view.get("focus-children-section").hidden, true);
  assert.equal(view.get("focus-create-section").hidden, true);
  await view.get("focus-create-form").submit();
  assert.deepEqual(view.writes(), []);
  assert.match(view.text("focus-lease"), /^Part of ReleaseCloses in 5m 0s · /);
  assert.deepEqual(view.requests.map(({path, params}) => [path, params.session]), [["/api/team", "leader-session"]]);
  view.get("focus-lease").children[0].children[1].click();
  assert.deepEqual(view.events.opened, ["room"]);

  const unknown = {...child, parent_room_id: "missing"};
  view.state.room = unknown;
  await view.api.refresh();
  assert.match(view.text("focus-lease"), /^Part of missing/);
});

test("a leader renews a due lease with the current deadline and retries the same request", async (t) => {
  let answer = {state: "unavailable"};
  const child = {...root, id: "child", name: "Investigation", parent_room_id: "room",
    created_at: past(290), expires_at: future(10)};
  const view = panel(t, {room: child, rooms: [root, child],
    respond: (_path, _params, action) => action ? answer : empty});
  await view.api.refresh();
  assert.equal(view.get("focus-extend").disabled, false);
  assert.match(view.text("focus-lease-hint"), /Renewal is open/);
  view.get("focus-extend-seconds").value = "1800";
  await view.get("focus-extend-form").submit();
  const body = {seconds: 1800, expected_expires_at: child.expires_at};
  assert.deepEqual(view.writes(), [["/api/room-lease", {room: "child", session: "leader-session"}, body]]);
  assert.match(view.text("focus-status"), /could not confirm/);
  assert.match(view.text("focus-lease-hint"), /unconfirmed\. Retry repeats the same request/);
  assert.equal(view.get("focus-extend-seconds").disabled, true);
  assert.equal(view.get("focus-extend").disabled, false);

  answer = {state: "ready", room_id: "child", expires_at: future(40)};
  await view.get("focus-extend-form").submit();
  assert.deepEqual(view.writes().map(([, , body]) => body), [body, body]);
  assert.equal(view.get("focus-extend-seconds").disabled, false);
  assert.match(view.text("focus-status"), /Lease extended\. Investigation now closes/);
  assert.equal(view.events.changed, 1);
});

test("an open lease, an expired lease and a non-leader cannot renew", async (t) => {
  const child = (expires_at, extra = {}) => ({...root, ...extra, id: "child", parent_room_id: "room",
    created_at: past(60), expires_at});
  const open = panel(t, {room: child(future(120))});
  await open.api.refresh();
  assert.equal(open.get("focus-extend").disabled, true);
  assert.match(open.text("focus-lease-hint"), /Renewal opens 15 minutes before/);
  const expired = panel(t, {room: child(past(1))});
  await expired.api.refresh();
  assert.equal(expired.get("focus-extend").disabled, true);
  assert.match(expired.text("focus-lease"), /Lease expired/);
  const member = panel(t, {room: child(future(5), {leader_member_id: "other",
    memberships: [{session: leader.id, acknowledged: 1, current: 1}]})});
  await member.api.refresh();
  assert.equal(member.get("focus-extend-form").hidden, true);
  assert.match(member.text("focus-lease-hint"), /The current room leader can renew/);
  assert.equal(member.requests.length, 0);
});

test("a leader invites a teammate from the existing team query", async (t) => {
  const team = {state: "ready", members: [
    {id: "lead", role: "Release lead"},
    {id: "peer", role: "Release peer", name: "Sam"},
    {id: "dana", role: "Designer", name: "Sam"},
    {id: "alex", role: "Ops", name: "Alex"},
  ]};
  let answer = {state: "unavailable"};
  const view = panel(t, {respond: (path, params, action) => {
    if (path === "/api/team") return team;
    return action ? answer : empty;
  }});
  await view.api.refresh();
  const member = view.get("focus-invite-member");
  assert.deepEqual(member.children.map(({value, textContent}) => [value, textContent]),
    [["peer", "Sam (peer)"], ["dana", "Sam (dana)"], ["alex", "Alex"]]);
  assert.equal(member.value, "peer");
  member.value = "dana";
  await view.get("focus-invite-form").submit();
  assert.match(view.text("focus-status"), /could not confirm/);
  answer = {state: "ready", dm_id: "dm", message_id: 9};
  await view.get("focus-invite-form").submit();
  const writes = view.writes();
  assert.deepEqual(writes.map(([path, params, body]) => [path, params, body.member_id]), [
    ["/api/room-invitations", {room: "room", session: "leader-session"}, "dana"],
    ["/api/room-invitations", {room: "room", session: "leader-session"}, "dana"],
  ]);
  assert.equal(writes[0][2].client_request_id, writes[1][2].client_request_id);
  assert.match(view.text("focus-status"), /Invitation sent to Sam \(dana\)/);
  assert.equal(member.value, "dana");

  member.value = "alex";
  answer = {state: "ready", dm_id: "dm", message_id: 10};
  await view.get("focus-invite-form").submit();
  assert.notEqual(view.writes()[2][2].client_request_id, writes[0][2].client_request_id);
  assert.deepEqual(view.requests.filter(({path}) => path === "/api/team").map(({params}) => params),
    [{session: "leader-session"}, {session: "leader-session"}, {session: "leader-session"}]);
});

test("members who do not lead the room cannot send invitations", async (t) => {
  const room = {...root, leader_member_id: "other",
    memberships: [{session: leader.id, acknowledged: 1, current: 1}]};
  const view = panel(t, {room});
  await view.api.refresh();
  assert.equal(view.get("focus-invite-form").hidden, true);
  await view.get("focus-invite-form").submit();
  assert.deepEqual(view.writes(), []);
  assert.match(view.text("focus-invite-hint"), /Only the current room leader/);
  assert.equal(view.requests.some(({path}) => path === "/api/team"), false);
});

test("unknown leadership is confirmed through the candidates read", async (t) => {
  const room = {id: "room", name: "Release", memberships: [{session: leader.id, acknowledged: 1, current: 1}]};
  const view = panel(t, {room, respond: (path) => path === "/api/room-leadership"
    ? {state: "ready", leadership_epoch: 4, candidates: []}
    : path === "/api/team" ? {state: "ready", members: [{id: "peer", role: "Release peer"}]} : empty});
  await view.api.refresh();
  assert.deepEqual(view.requests.find(({path}) => path === "/api/room-leadership").params,
    {room: "room", session: "leader-session"});
  assert.equal(view.get("focus-invite-form").hidden, false);
  assert.deepEqual(view.get("focus-invite-member").children.map(({value}) => value), ["peer"]);

  const denied = panel(t, {room, respond: (path) => path === "/api/room-leadership" ? {state: "forbidden"} : empty});
  await denied.api.refresh();
  assert.equal(denied.get("focus-invite-form").hidden, true);
  assert.equal(denied.requests.filter(({path}) => path === "/api/room-leadership").length, 1);
  assert.equal(denied.requests.some(({path}) => path === "/api/team"), false);

  const legacy = panel(t, {room: {...room, memberships: [{session: leader.id, can_remove_members: true}]},
    respond: (path) => path === "/api/team" ? {state: "ready", members: [{id: "peer", role: "Release peer"}]} : empty});
  await legacy.api.refresh();
  assert.equal(legacy.requests.some(({path}) => path === "/api/room-leadership"), false);
  assert.equal(legacy.get("focus-invite-form").hidden, false);
});

test("the child list reads through the selected actor and opens a child room", async (t) => {
  const pages = [
    {state: "ready", rooms: [{room_id: "child-b", name: "Beta", expires_at: future(90)}], next_after: "child-b"},
    {state: "ready", rooms: [{room_id: "child-a", name: "Alpha", archived_at: past(3), archive_reason: "lease_expired"}],
      next_after: null},
  ];
  const view = panel(t, {rooms: [root, {...root, id: "child-b"}],
    respond: (_path, params) => params.after ? pages[1] : pages[0]});
  await view.api.refresh();
  const list = view.get("focus-children");
  const [beta] = list.children;
  assert.match(beta.textContent, /^BetaCloses in 1h 30m · /);
  assert.equal(beta.children[1].textContent, "Open");
  assert.equal(view.get("focus-children-more").hidden, false);
  assert.equal(view.text("focus-children-status"), "");

  globalThis.document.activeElement = beta.children[1];
  await view.get("focus-children-more").click();
  assert.equal(list.children.length, 2);
  assert.match(list.children[1].children[0].textContent, /^AlphaClosed .*lease expired$/);
  assert.equal(list.children[1].children[1].textContent, "child-a");
  assert.equal(globalThis.document.activeElement, list.children[0].children[1]);
  assert.equal(view.get("focus-children-more").hidden, true);
  assert.deepEqual(view.requests.filter(({path}) => path === "/api/child-rooms").map(({params}) => params), [
    {after: null, room: "room", session: "leader-session"},
    {after: "child-b", room: "room", session: "leader-session"},
  ]);
  await view.api.refresh();
  assert.equal(view.requests.filter(({path}) => path === "/api/child-rooms").length, 2);
  beta.children[1].click();
  assert.deepEqual(view.events.opened, ["child-b"]);
});

test("an empty or unavailable child list reports its state and retries on the next refresh", async (t) => {
  let answer = {state: "unavailable"};
  const view = panel(t, {respond: () => answer});
  await view.api.refresh();
  assert.equal(view.text("focus-children-status"), "TeamUp could not load the child rooms.");
  assert.equal(view.get("focus-children-more").hidden, true);

  answer = {state: "unsupported"};
  await view.api.refresh();
  assert.equal(view.text("focus-children-status"), "This connection does not list child rooms.");

  answer = empty;
  await view.api.refresh();
  assert.equal(view.text("focus-children"), "No child rooms yet.");
  assert.equal(view.text("focus-children-status"), "");
  assert.equal(view.requests.filter(({path}) => path === "/api/child-rooms").length, 3);
});
