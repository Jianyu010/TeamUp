function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(text, action, className) {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

function actionError(state) {
  if (state === "not_found" || state === "member_retired") return "This session is no longer available.";
  if (state === "membership_changed") return "This session rejoined. Check its membership before trying again.";
  if (state === "not_registered" || state === "unauthorized") return "This session no longer has access.";
  return "TeamUp could not confirm the change. Please try again.";
}

function historyError(state) {
  if (state === "unauthorized") return "Sign in to TeamUp again to read archived conversations.";
  if (state === "not_found") return "This history is unavailable.";
  return "TeamUp could not load the history.";
}

export function sessionControls({request, changed, roomCommandError}) {
  const get = (id) => document.getElementById(id);
  const panel = get("retired-sessions");
  const retire = get("retire-identity");
  const confirm = get("retire-dialog");
  const archive = get("retired-history");
  const status = get("session-action-status");
  const selector = get("archive-conversation");
  const notices = get("archived-notices");
  let focused;
  let retiring;
  const busy = new Set();
  let subject;
  let conversations = [];
  let historyRead = 0;
  let retiredKey;

  async function act(session, room, membershipId) {
    if (busy.has(session.id)) return;
    busy.add(session.id);
    retire.disabled = true;
    status.textContent = room ? "Leaving room…" : "Retiring identity…";
    try {
      const path = room ? "/api/sessions/leave" : "/api/sessions/retire";
      const result = await request(path, {
        session: session.id, room: room?.id, membership_id: membershipId,
      }, {});
      if (result.state !== "ready") {
        status.textContent = roomCommandError(result.state) ?? actionError(result.state);
        return;
      }
      status.textContent = room
        ? session.label + " left " + room.name + "."
        : session.label + " moved to Retired sessions.";
      if (!room) panel.open = true;
      await changed(session, room, result, membershipId);
      if (!room) get("retired-summary").focus();
    } catch {
      status.textContent = actionError("unavailable");
    } finally {
      busy.delete(session.id);
      retire.disabled = Boolean(focused && busy.has(focused.id));
    }
  }

  function workspace(session, retired) {
    focused = session;
    retire.hidden = session?.state !== "ready";
    retire.disabled = Boolean(session && busy.has(session.id));
    const key = JSON.stringify(retired);
    if (key === retiredKey) return;
    retiredKey = key;
    get("retired-summary").textContent = "Retired sessions (" + retired.length + ")";
    const entries = retired.map((entry) => {
      const item = button(undefined, () => openArchive(entry), "node");
      const label = node("span");
      label.append(node("strong", entry.role_description),
        node("small", entry.harness + " · " + entry.member_id.slice(0, 8)));
      item.append(label);
      return item;
    });
    get("retired-list").replaceChildren(...(entries.length ? entries : [node("p", "No retired sessions", "muted")]));
    if (subject && !retired.some((entry) => entry.id === subject.id)) archive.close();
  }

  async function archived(params) {
    try {
      return await request("/api/retired", params);
    } catch {
      return {state: "unavailable"};
    }
  }

  async function openArchive(entry) {
    subject = entry;
    conversations = [];
    get("archive-title").textContent = entry.role_description;
    get("archive-status").textContent = "Reading archived conversations…";
    get("archive-messages").replaceChildren();
    get("archive-older").hidden = true;
    get("archive-picker").hidden = true;
    notices.replaceChildren();
    archive.showModal();

    const [listing, notificationPage] = await Promise.all([
      archived({session: entry.id}),
      archived({session: entry.id, after: "0"}),
    ]);
    if (subject !== entry) return;
    renderNotices(entry, notificationPage);
    if (listing.state !== "ready") {
      get("archive-status").textContent = historyError(listing.state);
      return;
    }
    conversations = listing.conversations;
    get("archive-picker").hidden = conversations.length === 0;
    selector.replaceChildren(...conversations.map((item, index) => {
      const label = (item.kind === "dm" ? "DM · " : "Room · ") + item.label;
      const option = node("option", label);
      option.value = String(index);
      return option;
    }));
    if (conversations.length) readHistory();
    else get("archive-status").textContent = "No archived conversations";
  }

  function renderNotices(entry, page, more = false) {
    if (page.state !== "ready") {
      notices.append(node("p", historyError(page.state), "muted"));
      return;
    }
    const rows = page.notifications.map((item) => node("p", item.kind === "room_left"
      ? "Left room " + item.room_id
      : item.kind === "dm"
        ? "DM notice · " + item.dm_id
        : "Mention in room " + item.room_id));
    if (!more && !rows.length) rows.push(node("p", "No recorded notifications", "muted"));
    notices.append(...rows);
    if (page.next_after) {
      const next = button("More notifications", async () => {
        next.disabled = true;
        const response = await archived({session: entry.id, after: page.next_after});
        if (subject !== entry) return;
        if (response.state === "ready") {
          next.remove();
          renderNotices(entry, response, true);
        } else {
          status.textContent = historyError(response.state);
          next.disabled = false;
        }
      });
      notices.append(next);
    }
  }

  async function readHistory(before) {
    const current = subject;
    const conversation = conversations[Number(selector.value)];
    if (!current || !conversation) return;
    const sequence = ++historyRead;
    const messages = get("archive-messages");
    const older = get("archive-older");
    if (!before) messages.replaceChildren();
    older.hidden = true;
    get("archive-status").textContent = "Reading history…";
    const page = await archived({
      session: current.id, kind: conversation.kind, conversation: conversation.conversation_id, before,
    });
    if (subject !== current || historyRead !== sequence) return;
    if (page.state !== "ready") {
      get("archive-status").textContent = historyError(page.state);
      const lostAccess = page.state === "unauthorized" || page.state === "not_found";
      if (lostAccess) messages.replaceChildren();
      older.hidden = lostAccess || !before;
      return;
    }
    get("archive-status").textContent = page.messages.length ? "" : "No retained messages";
    messages.prepend(...page.messages.map((message) => {
      const row = node("article", undefined, "archived-message");
      const author = message.sender_member_id === current.member_id ? current.role_description
        : (message.sender_retired ? "Retired member " : "Member ") + message.sender_member_id.slice(0, 8);
      const header = node("header");
      header.append(node("strong", author));
      if (message.created_at) {
        const date = node("time", new Date(message.created_at).toLocaleString());
        date.dateTime = message.created_at;
        header.append(date);
      }
      row.append(header, node("p", message.text));
      return row;
    }));
    older.dataset.before = page.older ?? "";
    older.hidden = !page.older;
  }

  retire.addEventListener("click", () => {
    retiring = focused;
    if (retiring?.state !== "ready") return;
    get("retire-description").textContent = retiring.label +
      " will leave all rooms and move to Retired sessions. This identity cannot be registered again.";
    confirm.showModal();
  });
  get("cancel-retirement").addEventListener("click", () => confirm.close());
  get("confirm-retirement").addEventListener("click", () => {
    confirm.close();
    if (retiring) act(retiring);
    retiring = null;
  });
  get("close-archive").addEventListener("click", () => archive.close());
  archive.addEventListener("close", () => {
    subject = null;
    get("archive-messages").replaceChildren();
    notices.replaceChildren();
  });
  selector.addEventListener("change", () => readHistory());
  get("archive-older").addEventListener("click", (event) => readHistory(event.currentTarget.dataset.before));

  return {workspace, leave: act};
}
