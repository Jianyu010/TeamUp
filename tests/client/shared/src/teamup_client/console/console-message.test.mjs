import assert from "node:assert/strict";
import test from "node:test";
import {messageRow} from "../../../../../../client/shared/src/teamup_client/console/console-history.mjs";

function render(message, disclosureState) {
  function node(tag, textContent, className) {
    return {
      tag,
      textContent,
      className,
      children: [],
      dataset: {},
      append(...items) { this.children.push(...items); },
      setAttribute(name, value) { this[name] = value; },
      addEventListener() {},
    };
  }
  return messageRow(node, {
    id: "9007199254740993",
    sender: "Release Coordinator",
    created_at: "2026-11-26T04:50:00Z",
    ...message,
  }, disclosureState);
}

test("room message separates ordered routing, title, and original body", () => {
  const body = "Review <draft>\n  keep this indentation\n";
  const row = render({
    mentioned_recipients: ["Reviewer One", "Reviewer Two"],
    text: "# November release\r\n\r\n" + body,
  });
  const [time, content] = row.children;
  assert.equal(time.dateTime, "2026-11-26T04:50:00Z");
  const [routing, title, text] = content.children;
  assert.equal(routing.className, "message-routing");
  assert.equal(routing.children[0].textContent, "Release Coordinator → Reviewer One, Reviewer Two");
  assert.equal(routing.children.length, 1);
  assert.equal(title.textContent, "msg-9007199254740993 · November release");
  assert.equal(text.textContent, body);
});

test("recipient overflow exposes the complete list in order", () => {
  const row = render({
    mentioned_recipients: ["Reviewer One", "Reviewer Two", "Reviewer Three"],
    text: "Body",
  }, {expanded: true});
  const [routing] = row.children[1].children;
  const [label, disclosure] = routing.children;
  const [details, recipients] = disclosure.children;
  assert.equal(label.textContent, "Release Coordinator → Reviewer One, Reviewer Two");
  assert.equal(details.tag, "details");
  assert.equal(details.open, true);
  assert.equal(details.dataset.id, "9007199254740993");
  const [summary] = details.children;
  assert.equal(details.children.length, 1);
  assert.equal(summary.tag, "summary");
  assert.equal(summary.textContent, "…");
  assert.equal(summary["aria-controls"], recipients.id);
  assert.equal(summary["aria-label"], "All recipients — Reviewer One, Reviewer Two, Reviewer Three");
  assert.equal(recipients.textContent, "Reviewer One, Reviewer Two, Reviewer Three");
});

test("ordinary messages keep the entire body and display the exact ID as their reference", () => {
  const plain = "First line\n\nSecond paragraph";
  const row = render({text: plain, mentioned: null, created_at: null});
  assert.equal(row.children[0].textContent, "—");
  const [routing, title, body] = row.children[1].children;
  assert.equal(routing.children[0].textContent, "Release Coordinator");
  assert.equal(title.textContent, "msg-9007199254740993");
  assert.equal(body.textContent, plain);
});

test("a leading hash reference without the heading separator stays in the body", () => {
  const text = "# Issue reference\nContinue on the next line.";
  const row = render({text, mentioned: "Single Reviewer"});
  const [routing, title, body] = row.children[1].children;
  assert.equal(routing.children[0].textContent, "Release Coordinator → Single Reviewer");
  assert.equal(title.textContent, "msg-9007199254740993");
  assert.equal(body.textContent, text);
});

test("a closed recipient disclosure restores recent hover and focus dismissals", () => {
  const row = render({
    mentioned_recipients: ["Reviewer One", "Reviewer Two", "Reviewer Three"],
    text: "Body",
  }, {dismissHover: true, dismissFocus: true});
  const disclosure = row.children[1].children[0].children[1];
  assert.equal(disclosure.children[0].open, false);
  assert.deepEqual(disclosure.dataset, {dismissHover: "", dismissFocus: ""});
});

test("legacy titles move below compact routing while full recipient labels remain in overflow", () => {
  const message = {
    sender: "Coordinator: PR review support",
    mentioned_recipients: ["Lead：AS decisions", "API: Owner", "Coordinator: Rollbacks"],
    routing: {
      sender: "Coordinator (93e57f02)",
      recipients: ["Lead", "API: Owner"],
      legacy_titles: ["PR review support", "AS decisions"],
    },
  };
  const row = render({...message, text: "Check #858"});
  const [routing, title, body] = row.children[1].children;
  const [labels, disclosure] = routing.children;
  assert.equal(labels.textContent, "Coordinator (93e57f02) → Lead, API: Owner");
  assert.equal(disclosure.children[1].textContent, "Lead：AS decisions, API: Owner, Coordinator: Rollbacks");
  assert.equal(title.textContent, "msg-9007199254740993 · PR review support · AS decisions");
  assert.equal(body.textContent, "Check #858");

  const titledRow = render({...message, text: "# Handoff status\n\nCheck #858"});
  const [, authoredTitle, details, authoredBody] = titledRow.children[1].children;
  assert.equal(authoredTitle.textContent, "msg-9007199254740993 · Handoff status");
  assert.equal(details.textContent, "PR review support · AS decisions");
  assert.equal(authoredBody.textContent, "Check #858");
});
