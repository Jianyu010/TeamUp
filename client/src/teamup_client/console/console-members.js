const get = (id) => document.getElementById(id);
const labels = {online: "Online", offline: "Offline", unknown: "Unknown"};
const heartbeatLifetime = 45_000;
const expiryTimers = new WeakMap();

function element(tag, text, css) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (css) item.className = css;
  return item;
}

function setPresence(badge, state) {
  badge.dataset.presence = state;
  badge.textContent = labels[state];
}

function presenceBadge(observation, requestedAt) {
  const badge = element("span", "", "presence");
  const observed = Date.parse(observation?.observed_at);
  const age = observation?.age_ms;
  let state = observation?.state;
  if (!["online", "offline"].includes(state) || !Number.isSafeInteger(age) || age < 0) state = "unknown";
  if (state === "online") {
    const deadline = requestedAt + heartbeatLifetime - age;
    if (deadline <= performance.now()) state = "unknown";
    else badge.dataset.deadline = String(deadline);
  }
  if (state !== "unknown") {
    const seconds = Math.floor((age + performance.now() - requestedAt) / 1000);
    badge.title = "Last heartbeat " + seconds + "s ago at refresh" +
      (Number.isFinite(observed) ? " · " + new Date(observed).toLocaleString() : "");
  }
  setPresence(badge, state);
  return badge;
}

function scheduleExpiry(list, onExpiry) {
  clearTimeout(expiryTimers.get(list));
  expiryTimers.delete(list);
  const now = performance.now();
  let next = Infinity;
  let expired = false;
  for (const badge of list.querySelectorAll(".presence[data-deadline]")) {
    const deadline = Number(badge.dataset.deadline);
    if (deadline <= now) {
      delete badge.dataset.deadline;
      badge.removeAttribute("title");
      setPresence(badge, "unknown");
      expired = true;
    } else next = Math.min(next, deadline);
  }
  if (Number.isFinite(next)) {
    expiryTimers.set(list, setTimeout(() => scheduleExpiry(list, onExpiry), Math.ceil(next - now)));
  }
  if (expired) onExpiry?.();
}

function memberNames(members) {
  const counts = new Map();
  for (const {role} of members) counts.set(role, (counts.get(role) || 0) + 1);
  return members.map(({id, role}) => {
    const name = element("span", "", "member-name");
    name.append(element("span", role));
    if (counts.get(role) > 1) {
      const prefix = id.slice(0, 8);
      const shared = members.some((other) => other.id !== id && other.role === role && other.id.startsWith(prefix));
      const qualifier = element("small", "(" + (shared ? id : prefix) + ")");
      qualifier.title = id;
      name.append(document.createTextNode(" "), qualifier);
    }
    return name;
  });
}

export function renderTeam(result, requestedAt) {
  const list = get("team-list");
  const members = result.members || [];
  const names = memberNames(members);
  list.replaceChildren(...members.map((member, index) => {
    const row = element("li");
    row.append(names[index], presenceBadge(member.presence, requestedAt));
    return row;
  }));
  scheduleExpiry(list);
}

export function roomMembers({read, subject, chat, policyChanged}) {
  const rail = get("roster");
  const panel = get("member-detail");
  let page = null;
  let request = null;
  let selected = null;
  let pinned = false;
  let dismissTimer;
  let rows = [];
  const recentCommunication = new Map();

  function close() {
    clearTimeout(dismissTimer);
    if (selected) {
      selected.setAttribute("aria-expanded", "false");
      selected.removeAttribute("aria-describedby");
    }
    selected = null;
    pinned = false;
    if (panel.matches(":popover-open")) panel.hidePopover();
    panel.replaceChildren();
  }

  function position() {
    if (!selected) return;
    const anchor = selected.getBoundingClientRect();
    const viewport = rail.getBoundingClientRect();
    if (anchor.bottom <= viewport.top || anchor.top >= viewport.bottom) return close();
    const left = Math.max(6, anchor.left - panel.offsetWidth - 6);
    const top = Math.max(6, Math.min(anchor.top, window.innerHeight - panel.offsetHeight - 6));
    panel.style.left = left + "px";
    panel.style.top = top + "px";
  }

  function details() {
    if (!selected) return;
    const member = page?.members?.find((item) => item.id === selected.dataset.member);
    if (!member) return close();
    const transcript = chat();
    const recent = recentCommunication.get(member.id)?.message;
    const receipt = member.acknowledged === page.current
      ? "Acknowledged version " + page.current : "Awaiting acknowledgment of version " + page.current;
    const content = [element("h3", selected.querySelector(".member-name").textContent), element("p", receipt), element("h4", "Recent communication")];
    if (recent) {
      const stamp = Date.parse(recent.created_at);
      content.push(element("p", recent.sender + " · " +
        (Number.isFinite(stamp) ? new Date(stamp).toLocaleString() : "Time unknown")));
      const excerpt = Array.from(recent.text);
      content.push(element("p", excerpt.length > 180 ? excerpt.slice(0, 180).join("") + "…" : recent.text));
    } else content.push(element("p", transcript?.state === "ready"
      ? "None in the loaded messages." : "Room history unavailable."));
    content.push(element("h4", "Current tasks"));
    if (member.tasks?.length) {
      const list = element("ul");
      for (const task of member.tasks) {
        const row = element("li", task.title);
        const others = task.teammates.map((person) => person.role);
        row.append(element("small", others.length ? "With " + others.join(", ") : "No teammates"));
        list.append(row);
      }
      content.push(list);
    } else content.push(element("p", Array.isArray(member.tasks) ? "No active tasks." : "Task details unavailable."));
    panel.replaceChildren(...content);
    if (!panel.matches(":popover-open")) panel.showPopover();
    position();
  }

  function open(button, pin = false) {
    clearTimeout(dismissTimer);
    if (pinned && !pin && selected !== button) return;
    if (selected) {
      selected.setAttribute("aria-expanded", "false");
      selected.removeAttribute("aria-describedby");
    }
    selected = button;
    selected.setAttribute("aria-expanded", "true");
    selected.setAttribute("aria-describedby", panel.id);
    pinned ||= pin;
    details();
  }

  function delayClose() {
    if (!pinned) {
      clearTimeout(dismissTimer);
      dismissTimer = setTimeout(close, 160);
    }
  }

  function reorder() {
    recentCommunication.clear();
    const transcript = chat();
    if (transcript?.state === "ready") {
      for (const [index, message] of transcript.messages.entries()) {
        const activity = {message, index};
        recentCommunication.set(message.sender_member_id, activity);
        for (const id of message.recipient_member_ids ?? []) recentCommunication.set(id, activity);
      }
    }

    const ordered = rows.map(({button, badge}) => ({
      button,
      online: Number(badge.dataset.presence === "online"),
      activity: recentCommunication.get(button.dataset.member)?.index ?? -1,
    }));
    ordered.sort((a, b) => b.online - a.online || b.activity - a.activity);
    const focused = document.activeElement;
    for (const [index, {button}] of ordered.entries()) {
      if (rail.children[index] !== button) rail.insertBefore(button, rail.children[index] || null);
    }
    if (focused && document.activeElement !== focused && rail.contains(focused)) {
      focused.focus({preventScroll: true});
    }
    details();
  }

  function reset() {
    close();
    page = null;
    request = null;
    rows = [];
    recentCommunication.clear();
    rail.replaceChildren();
    scheduleExpiry(rail);
    get("roster-title").textContent = "Members";
    get("roster-status").textContent = "";
    policyChanged();
  }

  function render(result, started) {
    page = result;
    const ready = result.state === "ready";
    get("roster-title").textContent = ready ? "Members (" + result.members.length + ")" : "Members";
    get("roster-status").textContent = !ready ? "Room members unavailable."
      : !result.members.length ? "No room members." : "";
    const existing = new Map(Array.from(rail.children, (button) => [button.dataset.member, button]));
    const members = result.members || [];
    const names = memberNames(members);
    rows = members.map((member, index) => {
      const button = existing.get(member.id) || element("button");
      existing.delete(member.id);
      button.type = "button";
      button.dataset.member = member.id;
      button.setAttribute("aria-controls", panel.id);
      button.setAttribute("aria-expanded", String(selected === button));
      const badge = presenceBadge(member.presence, started);
      button.replaceChildren(names[index], badge);
      return {button, badge};
    });
    for (const button of existing.values()) button.remove();
    reorder();
    scheduleExpiry(rail, reorder);
    policyChanged();
  }

  async function refresh() {
    const scope = subject();
    if (!scope || request) return;
    const started = performance.now();
    const pending = {};
    request = pending;
    if (!page) get("roster-status").textContent = "Reading room members…";
    try {
      const result = await read("/api/room-members", scope);
      if (request === pending) render(result, started);
    } catch {
      if (request === pending) render({state: "unavailable", members: []}, started);
    } finally {
      if (request === pending) request = null;
    }
  }

  rail.addEventListener("pointerover", (event) => {
    const button = event.target.closest("button");
    if (button && event.pointerType !== "touch") open(button);
  });
  rail.addEventListener("pointerleave", delayClose);
  rail.addEventListener("focusin", (event) => {
    const button = event.target.closest("button");
    if (button) {
      if (selected !== button) pinned = false;
      open(button);
    }
  });
  rail.addEventListener("focusout", delayClose);
  rail.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (selected === button && pinned) close();
    else open(button, true);
  });
  panel.addEventListener("pointerenter", () => clearTimeout(dismissTimer));
  panel.addEventListener("pointerleave", delayClose);
  document.addEventListener("pointerdown", (event) => {
    if (!rail.contains(event.target) && !panel.contains(event.target)) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && selected) {
      event.stopPropagation();
      event.preventDefault();
      close();
    }
  });
  rail.addEventListener("scroll", position, {passive: true});
  window.addEventListener("resize", position);
  return {refresh, reset, chatChanged: reorder, historyPolicy: () => page?.history_policy};
}
