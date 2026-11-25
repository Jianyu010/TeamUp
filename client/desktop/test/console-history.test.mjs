import assert from "node:assert/strict";
import test from "node:test";
import {readTranscript} from "../../src/teamup_client/console/console-history.mjs";

test("older pages and polls preserve survivors under a stable expiration watermark", async () => {
  const base = 90071992547409920n;
  const id = (n) => String(base + BigInt(n));
  const records = Array.from({length: 90}, (_, index) => ({id: id(index + 1), created_at: null}));
  records[40].created_at = "2026-11-25T12:00:00Z";
  const calls = [];
  async function fetchPage(cursor) {
    calls.push(cursor);
    const messages = cursor.after
      ? records.filter((row) => BigInt(row.id) > BigInt(cursor.after)).slice(0, 30)
      : records.filter((row) => !cursor.before || BigInt(row.id) < BigInt(cursor.before)).slice(-30);
    return {
      state: "ready", messages, board: {current: 1, acknowledged: 1},
      expired_through_message_id: id(91), history_truncated: true,
      older: !cursor.after && messages.length === 30 ? messages[0].id : null,
      more_recent: Boolean(cursor.after) && messages.length === 30,
    };
  }
  let {page} = await readTranscript(null, null, fetchPage);
  assert.deepEqual(page.messages, records.slice(60));
  ({page} = await readTranscript(page, page.older, fetchPage));
  assert.deepEqual(page.messages, records.slice(30));
  calls.length = 0;
  ({page} = await readTranscript(page, page.older, fetchPage));
  assert.deepEqual(page.messages, records);
  assert.deepEqual(calls, [{after: id(30)}, {after: id(60)}, {before: id(31)}]);

  // One timestamped row expires while the watermark and the legacy survivors remain.
  records.splice(40, 1);
  calls.length = 0;
  ({page} = await readTranscript(page, null, fetchPage));
  assert.deepEqual(page.messages, records);
  assert.equal(page.older, id(1));
  assert.equal(page.history_truncated, true);
  assert.deepEqual(calls, [{after: id(0)}, {after: id(30)}, {after: id(61)}, {after: id(90)}]);
});

test("polling refreshes retired sender labels on all loaded pages", async () => {
  const messages = Array.from({length: 60}, (_, index) => ({
    id: String(index + 1),
    sender: index === 48 ? "Backend" : "Reviewer",
  }));
  const board = {current: 1, acknowledged: 1};
  const previous = {state: "ready", board, messages, older: "1"};
  const current = messages.map((message) => message.id === "49"
    ? {...message, sender: "Retired member (93e57f02)"}
    : message);
  const {page, reset} = await readTranscript(previous, null, async ({after}) => {
    const messages = current.filter((message) => Number(message.id) > Number(after)).slice(0, 30);
    return {state: "ready", board, messages, more_recent: messages.length === 30};
  });

  assert.equal(reset, false);
  assert.equal(page.older, "1");
  assert.deepEqual(page.messages, current);
});

test("a denied intermediate probe discards the cached transcript", async () => {
  const previous = {
    state: "ready", board: {current: 1, acknowledged: 1},
    messages: [{id: "8"}, {id: "10"}], expired_through_message_id: "12",
  };
  const denied = {state: "board_ack_required", messages: []};
  const calls = [];
  const result = await readTranscript(previous, "8", async (cursor) => {
    calls.push(cursor);
    return calls.length === 1 ? {...previous, messages: [{id: "8"}], more_recent: true} : denied;
  });
  assert.deepEqual(calls, [{after: "7"}, {after: "8"}]);
  assert.deepEqual(result, {page: denied, reset: true});
});

test("a nonadvancing intermediate cursor rebases once", async () => {
  const previous = {
    state: "ready", board: {current: 1, acknowledged: 1},
    messages: [{id: "8"}, {id: "10"}], expired_through_message_id: "12",
  };
  const calls = [];
  const result = await readTranscript(previous, "8", async (cursor) => {
    calls.push(cursor);
    assert.ok(calls.length <= 3);
    return cursor.after ? {...previous, messages: [{id: "8"}], more_recent: true} : previous;
  });
  assert.deepEqual(calls, [{after: "7"}, {after: "8"}, {}]);
  assert.equal(result.reset, true);
  assert.deepEqual(result.page.messages, previous.messages);
});

test("a rejoin between transcript pages discards the previous membership's rows", async () => {
  const board = {current: 1, acknowledged: 1, membership_id: "old"};
  const previous = {state: "ready", board, messages: [{id: "5"}]};
  const current = {
    state: "ready", board: {...board, membership_id: "new"}, messages: [{id: "7"}],
  };
  const {page, reset} = await readTranscript(previous, null, async ({after}) =>
    after === "4" ? previous : current);
  assert.equal(reset, true);
  assert.deepEqual(page.messages, [{id: "7"}]);
});
