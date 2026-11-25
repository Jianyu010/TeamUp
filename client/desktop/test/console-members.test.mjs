import assert from "node:assert/strict";
import test from "node:test";
import {roomMembers} from "../../src/teamup_client/console/console-members.js";

class Element {
  dataset = {};
  children = [];

  addEventListener() {}
  setAttribute() {}

  append(...items) {
    this.children.push(...items);
  }

  replaceChildren(...items) {
    this.children = items;
  }

  insertBefore(item, next) {
    this.children = this.children.filter((child) => child !== item);
    this.children.splice(next ? this.children.indexOf(next) : this.children.length, 0, item);
  }

  querySelectorAll(selector) {
    assert.equal(selector, ".presence[data-deadline]");
    return this.children.flatMap((row) => row.children.filter((badge) =>
      badge.className === "presence" && Object.hasOwn(badge.dataset, "deadline")));
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

function room(t, members, messages) {
  const elements = new Map(["roster", "roster-title", "roster-status", "member-detail"]
    .map((id) => [id, new Element()]));
  const rail = elements.get("roster");
  globalThis.document = {
    getElementById: (id) => elements.get(id),
    createElement: () => new Element(),
    querySelectorAll: (selector) => rail.querySelectorAll(selector),
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
  });
  return {
    refresh: roster.refresh,
    orderedIds: () => rail.children.map((row) => row.dataset.member),
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

  roster.setTranscript({state: "ready", messages: [...messages, message("2", "b")]});
  assert.deepEqual(roster.orderedIds(), ["b", "a"]);

  roster.setTranscript({state: "board_ack_required", messages: []});
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);
});

test("an expired Online badge moves its member into the remaining group", async (t) => {
  const roster = room(t, [member("a", "online", 44990), member("b", "offline")], [message("1", "b")]);
  await roster.refresh();
  assert.deepEqual(roster.orderedIds(), ["a", "b"]);

  roster.advanceTime(10);
  assert.deepEqual(roster.orderedIds(), ["b", "a"]);
});
