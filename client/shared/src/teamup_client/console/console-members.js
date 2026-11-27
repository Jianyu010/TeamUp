const get = (id) => document.getElementById(id);
const labels = {online: "Online", offline: "Offline", unknown: "Unknown"};
const heartbeatLifetime = 45_000;
const expiryTimers = new WeakMap();
const nameOrder = new Intl.Collator(undefined, {sensitivity: "base"});

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

export function memberNames(members, nameTag = "span") {
  const names = members.map(({id, name, role}) => ({id, name: name ?? role}));
  const counts = new Map();
  for (const {name} of names) counts.set(name, (counts.get(name) || 0) + 1);
  return names.map(({id, name}) => {
    const view = element("span", "", "member-name");
    let label = name;
    view.append(element(nameTag, name));
    if (counts.get(name) > 1) {
      const prefix = id.slice(0, 8);
      const shared = names.some((other) => other.id !== id && other.name === name && other.id.startsWith(prefix));
      const suffix = "(" + (shared ? id : prefix) + ")";
      const qualifier = element("small", suffix);
      label += " " + suffix;
      qualifier.title = id;
      view.append(qualifier);
    }
    return {view, label};
  });
}

function uptimeLabel(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  if (seconds < 60) return seconds + "s";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + "m " + seconds % 60 + "s";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h " + minutes % 60 + "m";
  return Math.floor(hours / 24) + "d " + hours % 24 + "h";
}

export function renderTeam(result, requestedAt) {
  const list = get("team-list");
  const members = result.members || [];
  const names = memberNames(members);
  const rows = members.map((member, index) => {
    const row = element("tr");
    const identity = element("th");
    identity.scope = "row";
    names[index].view.title = names[index].label;
    const badge = presenceBadge(member.presence, requestedAt);
    identity.append(names[index].view, badge);
    const since = element("td", "—", "team-since");
    const duration = element("td", "—", "team-uptime");
    const start = member.presence?.online_since;
    const timestamp = Date.parse(start);
    const onlineFor = member.presence?.online_for_ms;
    if (badge.dataset.presence === "online" && Number.isFinite(timestamp) &&
        Number.isSafeInteger(onlineFor) && onlineFor >= 0) {
      const time = element("time", new Date(timestamp).toLocaleString());
      time.dateTime = start;
      since.replaceChildren(time);
      duration.textContent = uptimeLabel(onlineFor);
    }
    row.append(identity, element("td", member.owner_username ?? "—", "team-user"),
      element("td", member.description ?? "—"), since, duration);
    return {row, badge, since, duration, name: member.name ?? member.role, id: member.id};
  });
  const reorder = () => {
    const rank = ({badge}) => Number(badge.dataset.presence === "online");
    rows.sort((a, b) => rank(b) - rank(a) || nameOrder.compare(a.name, b.name) ||
      a.id.localeCompare(b.id));
    list.replaceChildren(...rows.map(({row}) => row));
  };
  reorder();
  scheduleExpiry(list, () => {
    for (const {badge, since, duration} of rows) {
      if (badge.dataset.presence !== "online") {
        since.textContent = "—";
        duration.textContent = "—";
      }
    }
    reorder();
  });
}

export function renderSessionChoices(chooser, sessions, selected, empty) {
  const rich = CSS.supports("appearance", "base-select");
  const options = sessions.map((session) => {
    const duplicate = sessions.some(({id, label}) => id !== session.id && label === session.label);
    const label = duplicate ? session.label + " · " + (session.member_id ?? session.id).slice(0, 8) : session.label;
    const option = new Option(label, session.id, false, session.id === selected);
    const owner = session.owner_username ? "by " + session.owner_username : null;
    if (owner) option.dataset.owner = owner;
    if (rich) {
      const name = element("span", label, "session-option-name");
      name.title = label;
      option.replaceChildren(name);
      if (owner) {
        option.setAttribute("aria-label", label);
        option.setAttribute("aria-description", owner);
        option.append(element("small", owner, "member-owner"));
      }
    } else option.title = owner ?? label;
    return option;
  });
  if (!options.length && empty) options.push(new Option(empty));
  if (rich) {
    const button = element("button");
    button.type = "button";
    button.append(element("selectedcontent"));
    chooser.replaceChildren(button, ...options);
  } else chooser.replaceChildren(...options);

  const owner = chooser.nextElementSibling;
  const showOwner = () => {
    owner.textContent = chooser.selectedOptions[0]?.dataset.owner ?? "";
    owner.hidden = rich || !owner.textContent;
  };
  chooser.onchange = showOwner;
  showOwner();
}

function actionIcon(kind) {
  const ns = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(ns, "svg");
  const path = document.createElementNS(ns, "path");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  path.setAttribute("d", kind === "leave"
    ? "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 4 5 5-5 5m5-5H9"
    : "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 9h6M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z");
  icon.append(path);
  return icon;
}

export function roomMembers({read, subject, chat, policyChanged, namesChanged, actionFor}) {
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

  function position(keepPinnedVisible = false) {
    if (!selected) return;
    let anchor = selected.getBoundingClientRect();
    const viewport = rail.getBoundingClientRect();
    if (anchor.bottom <= viewport.top || anchor.top >= viewport.bottom) {
      if (!keepPinnedVisible || !pinned) return close();
      rail.scrollTop += anchor.bottom <= viewport.top
        ? anchor.top - viewport.top : anchor.bottom - viewport.bottom;
      anchor = selected.getBoundingClientRect();
    }
    const left = Math.max(6, anchor.left - panel.offsetWidth - 6);
    const top = Math.max(6, Math.min(anchor.top, window.innerHeight - panel.offsetHeight - 6));
    panel.style.left = left + "px";
    panel.style.top = top + "px";
  }

  function details(keepPinnedVisible = false) {
    if (!selected) return;
    const member = page?.members?.find((item) => item.id === selected.dataset.member);
    if (!member) return close();
    const transcript = chat();
    const recent = recentCommunication.get(member.id)?.message;
    const receipt = member.acknowledged === page.current
      ? "Acknowledged version " + page.current : "Awaiting acknowledgment of version " + page.current;
    const content = [element("h3", selected.dataset.label)];
    if (member.owner_username) content.push(element("small", "by " + member.owner_username, "member-owner"));
    if (member.description) content.push(element("p", member.description, "member-responsibility"));
    content.push(element("p", receipt), element("h4", "Recent communication"));
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
    position(keepPinnedVisible);
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
      for (const [index, message] of (transcript.messages ?? []).entries()) {
        const activity = {message, index};
        recentCommunication.set(message.sender_member_id, activity);
        for (const id of message.recipient_member_ids ?? []) recentCommunication.set(id, activity);
      }
    }

    const ordered = rows.map(({row, badge}) => ({
      row,
      online: Number(badge.dataset.presence === "online"),
      activity: recentCommunication.get(row.dataset.member)?.index ?? -1,
    }));
    ordered.sort((a, b) => b.online - a.online || b.activity - a.activity);
    const focused = document.activeElement;
    let next = null;
    let moved = false;
    // Move siblings around the focused row to retain keyboard focus.
    for (const {row} of ordered.reverse()) {
      if (!row.contains(focused) && (row.parentElement !== rail || row.nextElementSibling !== next)) {
        rail.insertBefore(row, next);
        moved = true;
      }
      next = row;
    }
    details(moved);
  }

  function reset({preserveNames = false} = {}) {
    close();
    page = null;
    request = null;
    rows = [];
    recentCommunication.clear();
    rail.replaceChildren();
    scheduleExpiry(rail);
    get("roster-title").textContent = "Members";
    get("roster-status").textContent = "";
    if (!preserveNames) namesChanged?.(new Map());
    policyChanged();
  }

  function render(result, started) {
    page = result;
    const directory = result.state === "directory_only";
    const ready = result.state === "ready";
    get("roster-title").textContent = directory ? "Team directory (" + result.members.length + ")"
      : ready ? "Members (" + result.members.length + ")" : "Members";
    get("roster-status").textContent = directory ? "This connection does not expose room membership."
      : !ready ? "Room members unavailable."
      : !result.members.length ? "No room members." : "";
    const existing = new Map(rows.map((entry) => [entry.row.dataset.member, entry]));
    const members = result.members || [];
    const names = memberNames(members);
    rows = members.map((member, index) => {
      let entry = existing.get(member.id);
      if (!entry) {
        const row = element("div", undefined, "member-row");
        const button = element("button", undefined, "member-details");
        row.dataset.member = member.id;
        button.type = "button";
        button.dataset.member = member.id;
        button.setAttribute("aria-controls", panel.id);
        row.append(button);
        entry = {row, button, control: null};
      }
      existing.delete(member.id);
      entry.button.setAttribute("aria-expanded", String(selected === entry.button));
      entry.badge = presenceBadge(member.presence, started);
      entry.button.dataset.label = names[index].label;
      entry.button.replaceChildren(names[index].view, entry.badge);
      const action = directory ? null : actionFor?.(member);
      if (entry.control?.dataset.kind !== action?.kind) {
        entry.control?.remove();
        entry.control = null;
        if (action) {
          const label = action.kind === "leave" ? "Leave this room" : "Remove from room";
          entry.control = element("button", undefined, "member-action");
          entry.control.type = "button";
          entry.control.title = label;
          entry.control.setAttribute("aria-label", label);
          entry.control.dataset.kind = action.kind;
          entry.control.append(actionIcon(action.kind));
          entry.row.append(entry.control);
        }
      }
      if (entry.control) {
        const control = entry.control;
        control.onclick = async () => {
          control.disabled = true;
          try {
            await action.run();
          } finally {
            control.disabled = false;
          }
        };
      }
      return entry;
    });
    for (const {row} of existing.values()) row.remove();
    reorder();
    scheduleExpiry(rail, reorder);
    namesChanged?.(new Map(members.map(({id}, index) => [id, names[index].label])));
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
    const button = event.target.closest(".member-details");
    if (button && event.pointerType !== "touch") open(button);
  });
  rail.addEventListener("pointerleave", delayClose);
  rail.addEventListener("focusin", (event) => {
    const button = event.target.closest(".member-details");
    if (button) {
      if (selected !== button) pinned = false;
      open(button);
    }
  });
  rail.addEventListener("focusout", delayClose);
  rail.addEventListener("click", (event) => {
    const button = event.target.closest(".member-details");
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
  rail.addEventListener("scroll", () => position(), {passive: true});
  window.addEventListener("resize", () => position());
  return {refresh, reset, chatChanged: reorder, historyPolicy: () => page?.history_policy};
}
