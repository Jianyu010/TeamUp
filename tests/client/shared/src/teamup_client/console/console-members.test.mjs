import assert from "node:assert/strict";
import test from "node:test";
import {renderTeam, roomMembers} from "../../../../../../client/shared/src/teamup_client/console/console-members.js";

class Element {
  dataset = {};
  children = [];
  #text = "";

  get textContent() { return this.#text; }
  set textContent(value) {
    this.replaceChildren();
    this.#text = value;
  }

  get nextElementSibling() {
    const siblings = this.parentElement?.children;
    return siblings?.[siblings.indexOf(this) + 1] ?? null;
  }

  addEventListener() {}
  setAttribute(name, value) { this[name] = value; }

  matches(selector) {
    assert.equal(selector, ":popover-open");
    return false;
  }

  append(...items) {
    for (const item of items) item.parentElement = this;
    this.children.push(...items);
  }

  contains(item) {
    return this === item || this.children.some((child) => child.contains?.(item));
  }

  replaceChildren(...items) {
    for (const child of this.children) child.parentElement = null;
    for (const child of items) child.parentElement = this;
    this.children = items;
  }

  insertBefore(item, next) {
    if (document.activeElement === item) document.activeElement = null;
    const siblings = this.children.filter((child) => child !== item);
    siblings.splice(next ? siblings.indexOf(next) : siblings.length, 0, item);
    this.replaceChildren(...siblings);
  }

  querySelectorAll(selector) {
    assert.equal(selector, ".presence[data-deadline]");
    const descendants = (item) => (item.children ?? []).flatMap((child) => [child, ...descendants(child)]);
    return descendants(this).filter((badge) =>
      badge.className === "presence" && Object.hasOwn(badge.dataset, "deadline"));
  }

  remove() {
    this.parentElement.replaceChildren(...this.parentElement.children.filter((child) => child !== this));
  }

  removeAttribute(attribute) {
    delete this[attribute];
  }
}

function member(id, state, age_ms = 0) {
  return {id, role: id, presence: {state, age_ms}};
}

function message(id, sender_member_id, recipient_member_ids = []) {
  return {id, sender_member_id, recipient_member_ids, created_at: null};
}

function room(t, members, messages, options = {}) {
  const elements = new Map(["roster", "roster-title", "roster-status", "member-detail"]
    .map((id) => [id, new Element()]));
  const rail = elements.get("roster");
  globalThis.document = {
    getElementById: (id) => elements.get(id),
    createElement: () => new Element(),
    createElementNS: () => new Element(),
    createTextNode: (text) => ({textContent: text}),
    addEventListener() {},
  };
  globalThis.window = {addEventListener() {}};
  t.after(() => {
    delete globalThis.document;
    delete globalThis.window;
  });

  let now = 0;
  t.mock.method(performance, "now", () => now);
  t.mock.timers.enable({apis: ["setTimeout"]});
  let transcript = {state: "ready", messages};
  const roster = roomMembers({
    read: async () => ({state: "ready", members}),
    subject: () => ({room: "room", session: "session"}),
    chat: () => transcript,
    policyChanged() {},
    ...options,
  });
  return {
    refresh: roster.refresh,
    reset: roster.reset,
    orderedIds: () => rail.children.map((row) => row.dataset.member),
    action: (id) => rail.children.find((row) => row.dataset.member === id)?.children
      .find((child) => child.className === "member-action"),
    focus(id) {
      const row = rail.children.find((item) => item.dataset.member === id);
      document.activeElement = row.children[0];
    },
    focusedId: () => document.activeElement?.parentElement?.dataset.member,
    setTranscript(result) {
      transcript = result;
      roster.chatChanged();
    },
    advanceTime(ms) {
      now += ms;
      t.mock.timers.tick(ms);
    },
  };
}

test("room roster groups Online members and orders each group by visible communication", async (t) => {
  const roster = room(t, [
    member("a", "offline"), member("b", "online"), member("c", "offline"),
    member("d", "online"), member("e", "unknown"), member("f", "online"),
    member("g", "online"), member("h", "unknown"),
  ], [
    message("9007199254740992", "b"),
    message("9007199254740993", "c"),
    message("9007199254740994", "f", ["d"]),
    message("9007199254740995", "e", ["a"]),
  ]);

  await roster.refresh();
  assert.deepEqual(roster.orderedIds(), ["d", "f", "b", "g", "a", "e", "c", "h"]);
});

test("room roster updates as the authorized transcript changes", async (t) => {
  const messages = [message("1", "a")];
  const roster = room(t, [member("a", "online"), member("b", "online")], messages);
  await roster.refresh();
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);

  roster.focus("b");
  roster.setTranscript({state: "ready", messages: [...messages, message("2", "b")]});
  assert.deepEqual(roster.orderedIds(), ["b", "a"]);
  assert.equal(roster.focusedId(), "b");

  roster.setTranscript({state: "board_ack_required", messages: []});
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);

  roster.setTranscript({state: "ready"});
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);
});

test("an expired Online badge moves its member into the remaining group", async (t) => {
  const roster = room(t, [member("a", "online", 44990), member("b", "offline")], [message("1", "b")]);
  await roster.refresh();
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);

  roster.advanceTime(10);
  assert.deepEqual(roster.orderedIds(), ["b", "a"]);
});

test("room role labels reflect the current authorized roster and clear on reset", async (t) => {
  const reads = [];
  const updates = [];
  const roster = room(t, [], [], {
    read: () => new Promise((resolve) => reads.push(resolve)),
    namesChanged: (names) => updates.push(names),
  });
  const firstRead = roster.refresh();
  reads.shift()({state: "ready", members: [
    {id: "11111111-old", role: "Coordinator"},
    {id: "22222222-old", role: "Coordinator"},
    {id: "33333333-old", role: "Reviewer"},
  ]});
  await firstRead;
  assert.deepEqual(updates.at(-1), new Map([
    ["11111111-old", "Coordinator (11111111)"],
    ["22222222-old", "Coordinator (22222222)"],
    ["33333333-old", "Reviewer"],
  ]));

  const oldRead = roster.refresh();
  const oldResponse = reads.shift();
  roster.reset();
  assert.deepEqual(updates.at(-1), new Map());

  const nextRead = roster.refresh();
  reads.shift()({state: "ready", members: [{id: "44444444-new", role: "Editor"}]});
  await nextRead;
  oldResponse({state: "ready", members: [{id: "33333333-old", role: "Reviewer"}]});
  await oldRead;
  assert.deepEqual(updates.at(-1), new Map([["44444444-new", "Editor"]]));

  const deniedRead = roster.refresh();
  reads.shift()({state: "not_found", members: []});
  await deniedRead;
  assert.deepEqual(updates.at(-1), new Map());
});

test("a board refresh preserves known role labels while the roster reloads", async (t) => {
  const updates = [];
  const roster = room(t, [{id: "member", role: "Reviewer"}], [], {
    namesChanged: (names) => updates.push(names),
  });
  await roster.refresh();
  roster.reset({preserveNames: true});

  assert.deepEqual(updates, [new Map([["member", "Reviewer"]])]);
  assert.deepEqual(roster.orderedIds(), []);
});

test("the roster renders icon actions only on eligible rows", async (t) => {
  const called = [];
  const roster = room(t, [
    member("mine", "online"), member("target", "online"), member("other", "online"),
  ], [], {
    actionFor: ({id}) => ({
      mine: {kind: "leave", run: () => called.push("mine")},
      target: {kind: "remove", run: () => called.push("target")},
    })[id],
  });
  await roster.refresh();
  const leave = roster.action("mine");
  assert.equal(leave?.title, "Leave this room");
  assert.equal(leave["aria-label"], leave.title);
  assert.equal(leave.children.length, 1);
  assert.equal(roster.action("target")?.title, "Remove from room");
  assert.equal(roster.action("other"), undefined);
  leave.onclick();
  assert.deepEqual(called, ["mine"]);
});

test("Team ranks Online first, sorts names, and clears uptime at local expiry", (t) => {
  const list = new Element();
  globalThis.document = {
    getElementById: () => list,
    createElement: () => new Element(),
  };
  t.after(() => { delete globalThis.document; });
  let now = 0;
  t.mock.method(performance, "now", () => now);
  t.mock.timers.enable({apis: ["setTimeout"]});
  const onlineSince = "2026-11-25T12:00:00Z";
  const online = (id, name, duration, age_ms = 0) => ({
    ...member(id, "online", age_ms),
    name,
    presence: {
      state: "online", age_ms, observed_at: onlineSince,
      online_since: onlineSince, online_for_ms: duration,
    },
  });
  const members = [
    member("alfa", "offline"), online("zed", "Zed", 59_000),
    online("beta", "Beta", 60_000, 44_000),
    online("room", "Room", 3_999_999), online("days", "Days", 262_800_000),
    online("old", "Old Server", undefined), member("AAA", "unknown"),
  ];
  const values = () => list.children.map(({children}) => [
    children[0].children[0].children[0].textContent,
    children[3]?.children[0]?.dateTime ?? children[3]?.textContent,
    children[4]?.textContent,
  ]);
  renderTeam({members}, now);
  assert.deepEqual(values(), [
    ["Beta", onlineSince, "1m 0s"], ["Days", onlineSince, "3d 1h"],
    ["Old Server", "—", "—"], ["Room", onlineSince, "1h 6m"],
    ["Zed", onlineSince, "59s"], ["AAA", "—", "—"], ["alfa", "—", "—"],
  ]);
  now += 1_000;
  t.mock.timers.tick(1_000);
  assert.deepEqual(values().at(-1), ["Beta", "—", "—"]);
  renderTeam({members: [...members.filter(({id}) => id !== "beta"),
    online("beta", "Beta", 62_000)]}, now);
  assert.deepEqual(values()[0], ["Beta", onlineSince, "1m 2s"]);
});
