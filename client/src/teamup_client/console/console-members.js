const get = (id) => document.getElementById(id);
const labels = {online: "Online", offline: "Offline", unknown: "Unknown"};
const heartbeatLifetime = 45_000;
let expiryTimer;

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

function scheduleExpiry() {
  clearTimeout(expiryTimer);
  const now = performance.now();
  let next = Infinity;
  for (const badge of document.querySelectorAll(".presence[data-deadline]")) {
    const deadline = Number(badge.dataset.deadline);
    if (deadline <= now) {
      delete badge.dataset.deadline;
      badge.removeAttribute("title");
      setPresence(badge, "unknown");
    } else next = Math.min(next, deadline);
  }
  if (Number.isFinite(next)) expiryTimer = setTimeout(scheduleExpiry, Math.ceil(next - now));
}

export function renderTeam(result, requestedAt) {
  get("team-list").replaceChildren(...(result.members || []).map((member) => {
    const row = element("li");
    row.append(element("span", member.role), presenceBadge(member.presence, requestedAt));
    return row;
  }));
  scheduleExpiry();
}

export function roomMembers({read, subject, chat, policyChanged}) {
  const rail = get("roster");
  const panel = get("member-detail");
  let page = null;
  let request = null;
  let selected = null;
  let pinned = false;
  let dismissTimer;

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
    const recent = transcript?.messages?.findLast((item) =>
      item.sender_member_id === member.id || item.recipient_member_ids?.includes(member.id));
    const receipt = member.acknowledged === page.current
      ? "Acknowledged version " + page.current : "Awaiting acknowledgment of version " + page.current;
    const content = [element("h3", member.role), element("p", receipt), element("h4", "Recent communication")];
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

  function reset() {
    close();
    page = null;
    request = null;
    rail.replaceChildren();
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
    for (const [index, member] of (result.members || []).entries()) {
      const button = existing.get(member.id) || element("button");
      existing.delete(member.id);
      button.type = "button";
      button.dataset.member = member.id;
      button.setAttribute("aria-controls", panel.id);
      button.setAttribute("aria-expanded", String(selected === button));
      button.replaceChildren(element("span", member.role, "member-name"),
        presenceBadge(member.presence, started));
      if (rail.children[index] !== button) rail.insertBefore(button, rail.children[index] || null);
    }
    for (const button of existing.values()) button.remove();
    details();
    scheduleExpiry();
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
  return {refresh, reset, details, historyPolicy: () => page?.history_policy};
}
