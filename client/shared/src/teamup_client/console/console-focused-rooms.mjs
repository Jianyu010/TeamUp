const leadershipPath = "/api/room-leadership";
const childPath = "/api/child-rooms";
const invitationPath = "/api/room-invitations";
const leasePath = "/api/room-lease";
const teamPath = "/api/team";
const noticeWindow = 15 * 60_000;
const reasons = {
  leader_profile: "Leadership role",
  same_owner: "Same account",
  recent_activity: "Recent room activity",
};
const failures = {
  forbidden: "This session no longer leads the room.",
  not_found: "The room or member is unavailable to the selected session.",
  conflict: "The room, lease or request changed. Review the current state and try again.",
  membership_changed: "This session rejoined the room. Check its membership before trying again.",
  room_archived: "This room is archived.",
  member_retired: "This session is no longer available.",
  not_registered: "Register the selected session first.",
  unauthorized: "This session is no longer authorized.",
  invalid_request: "Check the entries and try again.",
  unsupported: "This connection does not support focused room actions.",
  unavailable: "TeamUp could not confirm the request. A retry repeats the same request.",
};
const changed = ["conflict", "forbidden", "not_found", "membership_changed", "room_archived",
  "member_retired", "not_registered", "unauthorized"];

function localDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function duration(milliseconds) {
  const seconds = Math.max(1, Math.ceil(milliseconds / 1000));
  if (seconds < 60) return seconds + "s";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + "m " + seconds % 60 + "s";
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? hours + "h " + minutes % 60 + "m" : Math.floor(hours / 24) + "d " + hours % 24 + "h";
}

function leaseText(room) {
  if (room.archived_at) {
    return "Closed " + localDate(room.archived_at) +
      (room.archive_reason === "lease_expired" ? " · lease expired" : "");
  }
  const renewal = leaseWindow(room.expires_at);
  return renewal === "expired" || renewal === "unknown" ? "Lease expired"
    : "Closes in " + duration(Date.parse(room.expires_at) - Date.now()) + " · " + localDate(room.expires_at);
}

export function isRoomLeader(room, session, membership) {
  if (!session || !membership) return false;
  return Number.isInteger(room.leadership_epoch)
    ? room.leader_member_id === session.member_id
    : membership.can_remove_members === true;
}

export function leadershipKnown(room) {
  return Number.isInteger(room?.leadership_epoch) ||
    room?.memberships.some(({can_remove_members}) => can_remove_members === true) === true;
}

export function leaseWindow(expiresAt, now = Date.now()) {
  const remaining = Date.parse(expiresAt) - now;
  if (Number.isNaN(remaining)) return "unknown";
  if (remaining <= 0) return "expired";
  return remaining <= noticeWindow ? "due" : "open";
}

export function leadershipDialog({get, node, request, departed, refresh}) {
  const dialog = get("leadership-dialog");
  const select = get("leadership-candidate");
  const detail = get("leadership-detail");
  const status = get("leadership-status");
  const dismiss = get("leadership-dismiss");
  const choose = get("leadership-choose");
  const auto = get("leadership-auto");
  const defer = get("leadership-defer");
  const retry = get("leadership-retry");
  const footer = get("session-action-status");
  let subject;
  let pending;

  function updateActions() {
    const candidate = subject?.candidates.find((entry) => entry.member_id === select.value);
    const available = Number.isInteger(subject?.epoch) && !subject.busy;
    select.disabled = !available;
    choose.disabled = !available || !candidate;
    auto.disabled = defer.disabled = !available;
    dismiss.disabled = subject?.busy === true;
    detail.textContent = candidate
      ? [reasons[candidate.reason], candidate.last_active_at && "Last active " + localDate(candidate.last_active_at)]
        .filter(Boolean).join(" · ")
      : "";
  }

  async function load(current, message = "") {
    current.epoch = null;
    current.candidates = [];
    select.replaceChildren();
    select.value = "";
    retry.hidden = true;
    status.textContent = "Reading eligible successors…";
    updateActions();
    let result;
    try {
      result = await request(leadershipPath, current.scope);
    } catch {
      result = {state: "unavailable"};
    }
    if (subject !== current || !dialog.open) return;
    if (result.state !== "ready") {
      retry.hidden = false;
      status.textContent = result.state === "forbidden"
        ? "This session no longer leads the room."
        : "TeamUp could not read eligible successors.";
      return;
    }
    current.epoch = result.leadership_epoch;
    current.candidates = result.candidates;
    const placeholder = node("option", "Choose a successor");
    placeholder.value = "";
    select.replaceChildren(placeholder, ...result.candidates.map((candidate, index) => {
      const name = candidate.name || candidate.role_description || candidate.member_id;
      const label = (index + 1) + ". " + name;
      const option = node("option", label);
      option.value = candidate.member_id;
      return option;
    }));
    select.value = "";
    status.textContent = message || (result.candidates.length ? "" : "No eligible successors are currently available.");
    updateActions();
  }

  async function submit(mode) {
    const current = subject;
    if (!current || current.busy || !Number.isInteger(current.epoch)) return;
    const body = {leadership_epoch: current.epoch, mode, leave: true};
    if (mode === "choose") {
      const candidate = current.candidates.find((item) => item.member_id === select.value);
      if (!candidate) return;
      body.member_id = candidate.member_id;
      body.membership_id = candidate.membership_id;
    }
    current.busy = true;
    status.textContent = mode === "defer" ? "Recording the deferred decision…" : "Updating room leadership…";
    updateActions();
    let result;
    try {
      result = await request(leadershipPath, current.scope, {body});
    } catch {
      result = {state: "unavailable"};
    }
    if (subject !== current || !dialog.open) return;
    current.busy = false;
    if (result.state === "ready") {
      const {session, room, membershipId} = current;
      close();
      if (result.left === true) {
        pending = null;
        footer.textContent = session.label + " left " + room.name + ".";
        await departed(session, room, result, membershipId);
      } else {
        pending = result.pending ? current : null;
        footer.textContent = result.pending
          ? "Leadership decision pending. " + session.label + " is still in the room."
          : session.label + " is still in the room.";
        await refresh();
      }
      return;
    }
    if (["conflict", "membership_changed", "forbidden", "room_archived", "not_found"].includes(result.state)) {
      await load(current, "The room or its candidates changed. Review the refreshed choices.");
      await refresh();
      return;
    }
    status.textContent = "TeamUp could not confirm the handoff. Try again.";
    updateActions();
  }

  function close() {
    subject = null;
    dialog.close();
  }

  dismiss.addEventListener("click", close);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!subject?.busy) close();
  });
  select.addEventListener("change", updateActions);
  choose.addEventListener("click", () => submit("choose"));
  auto.addEventListener("click", () => submit("auto"));
  defer.addEventListener("click", () => submit("defer"));
  retry.addEventListener("click", () => subject && load(subject));

  return {
    async open(session, room, membershipId) {
      if (dialog.open) return;
      subject = {
        session, room, membershipId, scope: {session: session.id, room: room.id},
        candidates: [], epoch: null, busy: false,
      };
      get("leadership-session").textContent = session.label;
      get("leadership-room-name").textContent = room.name;
      dialog.showModal();
      await load(subject);
    },
    observe(workspace) {
      if (!pending) return;
      const {session, room} = pending;
      const sessions = workspace.sessions.filter(({id}) => id === session.id);
      const member = workspace.rooms.find(({id}) => id === room.id)
        ?.memberships.some(({session: id}) => id === session.id);
      if (member ? sessions.length > 0 : !sessions.some(({state}) => state === "ready")) return;
      footer.textContent = session.label + (member
        ? " is no longer available. Check its membership in " + room.name + "."
        : " is no longer in " + room.name + ".");
      pending = null;
    },
  };
}

export function focusedRoomPanel(context) {
  const {get, node, request} = context;
  const status = get("focus-status");
  const leaseSection = get("focus-lease-section");
  const lease = get("focus-lease");
  const leaseHint = get("focus-lease-hint");
  const extendForm = get("focus-extend-form");
  const extendSeconds = get("focus-extend-seconds");
  const extend = get("focus-extend");
  const childrenSection = get("focus-children-section");
  const children = get("focus-children");
  const childrenStatus = get("focus-children-status");
  const childrenMore = get("focus-children-more");
  const inviteForm = get("focus-invite-form");
  const inviteMember = get("focus-invite-member");
  const invite = get("focus-invite");
  const inviteHint = get("focus-invite-hint");
  const createSection = get("focus-create-section");
  const createForm = get("focus-create-form");
  const createName = get("focus-create-name");
  const createRules = get("focus-create-rules");
  const createProtocol = get("focus-create-protocol");
  const createHistory = get("focus-create-history");
  const create = get("focus-create");
  const regions = [lease, children, inviteMember];
  const drawn = new WeakMap();
  const requestIds = new Map();
  let scope = null;
  let leadState = "unknown";
  let rows = null;
  let nextAfter = null;
  let reading = false;
  let read = 0;
  let team;
  let teamRead = 0;
  let teamLoading = false;
  let extension = null;
  let busy = false;

  function clear() {
    read++;
    teamRead++;
    scope = null;
    rows = null;
    nextAfter = null;
    reading = false;
    team = undefined;
    teamLoading = false;
    extension = null;
    leadState = "unknown";
    regions.forEach((region) => drawn.delete(region));
    children.replaceChildren();
    childrenStatus.textContent = "";
    inviteMember.replaceChildren();
    inviteMember.value = "";
  }

  function enter(room, actor) {
    clear();
    scope = {room: room.id, actor: actor.id};
  }

  function leader(room, actor) {
    if (!room || !actor) return false;
    const membership = room.memberships.find((join) => join.session === actor.id);
    return isRoomLeader(room, actor, membership) ||
      (scope?.room === room.id && leadState === "leader");
  }

  function button(key, text, action, className) {
    const control = node("button", text, className);
    control.type = "button";
    control.dataset.focusKey = key;
    control.addEventListener("click", action);
    return control;
  }

  function draw(region, key, rows) {
    if (drawn.get(region) === key) return;
    const active = globalThis.document?.activeElement;
    const focus = active?.dataset?.focusKey;
    const restore = focus && region.contains(active);
    drawn.set(region, key);
    region.replaceChildren(...rows());
    if (restore) {
      Array.from(region.children).flatMap((item) => item.children ?? [])
        .find(({dataset}) => dataset?.focusKey === focus)?.focus({preventScroll: true});
    }
  }

  function render() {
    if (!scope) return;
    const room = context.room();
    const actor = context.actor();
    if (room?.id !== scope.room || actor?.id !== scope.actor) return;
    const leads = leader(room, actor);
    const child = Boolean(room.parent_room_id);
    leaseSection.hidden = !child;
    childrenSection.hidden = child;
    createSection.hidden = child;
    if (child) renderLease(room, leads);
    create.disabled = busy;
    inviteForm.hidden = !leads;
    inviteHint.textContent = leads ? "" : "Only the current room leader can send invitations.";
    if (leads) renderInvite(room);
    invite.disabled = busy || !leads || !inviteMember.value;
    if (rows) renderChildren();
  }

  function renderLease(room, leads) {
    const parentRoom = context.rooms().find(({id}) => id === room.parent_room_id);
    const text = leaseText(room);
    const renewal = leaseWindow(room.expires_at);
    const retrying = extension !== null;
    draw(lease, JSON.stringify([parentRoom?.id, parentRoom?.name, text]), () => {
      const parent = node("p");
      parent.append("Part of ", parentRoom
        ? button(parentRoom.id, parentRoom.name, () => context.openRoom(parentRoom.id), "focus-link")
        : node("code", room.parent_room_id));
      return [parent, node("p", text)];
    });
    extendForm.hidden = !leads;
    extendSeconds.disabled = retrying;
    extend.disabled = busy || !(retrying || renewal === "due");
    leaseHint.textContent = retrying
      ? "An earlier extension is unconfirmed. Retry repeats the same request."
      : !leads
        ? "The current room leader can renew the lease in the last 15 minutes."
        : renewal === "due"
          ? "Renewal is open until the lease expires."
          : renewal === "expired"
            ? "An expired lease cannot be renewed."
            : "Renewal opens 15 minutes before the lease expires.";
  }

  function renderChildren() {
    const available = new Set(context.rooms().map(({id}) => id));
    const entries = rows.map((child) => [child, leaseText(child), available.has(child.room_id)]);
    draw(children, JSON.stringify(entries), () => entries.length ? entries.map(([child, text, open]) => {
      const item = node("li", undefined, "focus-child");
      const summary = node("div");
      summary.append(node("strong", child.name), node("small", text));
      item.append(summary, open
        ? button(child.room_id, "Open", () => context.openRoom(child.room_id), "focus-open")
        : node("small", child.room_id));
      return item;
    }) : [node("li", "No child rooms yet.", "muted")]);
    childrenMore.hidden = nextAfter === null;
    childrenStatus.textContent = "";
  }

  function memberLabel(member, members) {
    const name = member.name || member.role;
    return members.some((other) => other !== member && (other.name || other.role) === name)
      ? name + " (" + member.id.slice(0, 8) + ")" : name;
  }

  function renderInvite(room) {
    const local = new Set(room.memberships.flatMap(({session}) => context.sessions()
      .filter(({id}) => id === session).map(({member_id}) => member_id)));
    const eligible = (team || []).filter(({id}) => !local.has(id));
    const selected = inviteMember.value;
    const phase = team === undefined ? "reading" : team ? "ready" : "failed";
    draw(inviteMember, JSON.stringify([phase, eligible]), () => eligible.length ? eligible.map((member) => {
      const option = node("option", memberLabel(member, eligible));
      option.value = member.id;
      return option;
    }) : [node("option", {reading: "Reading team members…", ready: "No other team members",
      failed: "Team members are unavailable."}[phase])]);
    inviteMember.value = eligible.some(({id}) => id === selected) ? selected : eligible[0]?.id ?? "";
  }

  async function probe() {
    const current = scope;
    leadState = "checking";
    let result;
    try {
      result = await request(leadershipPath, {room: current.room, session: current.actor});
    } catch {
      result = {state: "unavailable"};
    }
    if (scope !== current) return;
    leadState = result.state === "ready" ? "leader" : result.state === "forbidden" ? "member" : "unknown";
    render();
    if (leadState === "leader" && team == null && !teamLoading) await loadTeam();
  }

  async function loadTeam() {
    const current = scope;
    const requestNo = ++teamRead;
    teamLoading = true;
    let result;
    try {
      result = await request(teamPath, {session: current.actor});
    } catch {
      result = {state: "unavailable"};
    }
    if (scope !== current || requestNo !== teamRead) return;
    teamLoading = false;
    team = result.state === "ready" && Array.isArray(result.members) ? result.members : null;
    render();
  }

  async function loadChildren(more) {
    const current = scope;
    const requestNo = ++read;
    reading = true;
    if (!more && !rows) childrenStatus.textContent = "Reading child rooms…";
    let result;
    try {
      result = await request(childPath, {room: current.room, session: current.actor, after: more ? nextAfter : null});
    } catch {
      result = {state: "unavailable"};
    }
    if (scope !== current || requestNo !== read) return;
    reading = false;
    if (result.state !== "ready" || !Array.isArray(result.rooms)) {
      if (!more) {
        rows = null;
        drawn.delete(children);
        children.replaceChildren();
        childrenStatus.textContent = result.state === "unsupported"
          ? "This connection does not list child rooms."
          : ["forbidden", "not_found"].includes(result.state)
            ? "Child rooms are unavailable to the selected session."
            : "TeamUp could not load the child rooms.";
      }
      childrenMore.hidden = true;
      return;
    }
    rows = more ? [...rows, ...result.rooms] : result.rooms;
    nextAfter = result.next_after ?? null;
    render();
  }

  function loadList() {
    return scope && rows === null && !reading && !context.room()?.parent_room_id
      ? loadChildren(false) : null;
  }

  async function write({working, run, settled, done}) {
    const current = scope;
    busy = true;
    render();
    status.textContent = working;
    try {
      let result;
      try {
        result = await run();
      } catch {
        result = {state: "unavailable"};
      }
      if (scope !== current) return;
      if (result.state === "ready") {
        settled();
        await done(result);
      } else {
        if (result.state !== "unavailable") settled();
        status.textContent = failures[result.state] ?? failures.unavailable;
        if (changed.includes(result.state)) {
          rows = null;
          leadState = "unknown";
          await context.changed?.();
          await loadList();
        }
      }
    } finally {
      busy = false;
      render();
    }
  }

  function retryId(key) {
    if (!requestIds.has(key)) requestIds.set(key, crypto.randomUUID());
    return requestIds.get(key);
  }

  async function createChild() {
    const room = context.room();
    const actor = context.actor();
    if (busy || !scope || !actor || room.parent_room_id) return;
    const draft = {name: createName.value.trim(), history_policy: createHistory.value};
    for (const [field, key] of [[createRules, "rules"], [createProtocol, "protocol"]]) {
      if (field.value.trim()) draft[key] = field.value.trim();
    }
    if (!draft.name) {
      status.textContent = "Enter a name for the child room.";
      createName.focus();
      return;
    }
    const key = JSON.stringify([actor.id, room.id, draft]);
    const body = {...draft, client_request_id: retryId(key)};
    await write({
      working: "Creating child room…",
      run: () => request(childPath, {room: room.id, session: actor.id}, {body}),
      settled: () => requestIds.delete(key),
      done: async (result) => {
        createName.value = createRules.value = createProtocol.value = "";
        createHistory.value = "since_joining";
        rows = null;
        status.textContent = result.name + " was created. Its lease ends " + localDate(result.expires_at) + ".";
        await context.changed?.();
        await loadList();
      },
    });
  }

  async function sendInvitation() {
    const room = context.room();
    const actor = context.actor();
    const member = inviteMember.value;
    if (busy || !scope || !actor || !member || !leader(room, actor)) return;
    const key = JSON.stringify([actor.id, room.id, member]);
    const body = {member_id: member, client_request_id: retryId(key)};
    const name = (team || []).find(({id}) => id === member);
    await write({
      working: "Sending invitation…",
      run: () => request(invitationPath, {room: room.id, session: actor.id}, {body}),
      settled: () => requestIds.delete(key),
      done: async () => {
        status.textContent = "Invitation sent to " + (name ? memberLabel(name, team) : member) + ".";
        await loadTeam();
      },
    });
  }

  async function extendLease() {
    const room = context.room();
    const actor = context.actor();
    if (busy || !scope || !actor || !room.expires_at) return;
    if (!extension && !leader(room, actor)) return;
    extension ??= {seconds: Number(extendSeconds.value), expected_expires_at: room.expires_at};
    const body = extension;
    await write({
      working: "Extending the lease…",
      run: () => request(leasePath, {room: room.id, session: actor.id}, {body}),
      settled: () => {
        extension = null;
      },
      done: async (result) => {
        status.textContent = "Lease extended. " + room.name + " now closes " + localDate(result.expires_at) + ".";
        await context.changed?.();
      },
    });
  }

  async function refresh() {
    if (!context.active()) return;
    const room = context.room();
    const actor = context.actor();
    if (!room || !actor) {
      if (scope) clear();
      return;
    }
    if (scope?.room !== room.id || scope.actor !== actor.id) enter(room, actor);
    render();
    const leads = leader(room, actor);
    await Promise.all([
      !leads && !leadershipKnown(room) && leadState === "unknown" ? probe() : null,
      loadList(),
      leads && team == null && !teamLoading ? loadTeam() : null,
    ]);
  }

  function reset() {
    clear();
    status.textContent = "";
  }

  extendForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await extendLease();
  });
  childrenMore.addEventListener("click", () => loadChildren(true));
  inviteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendInvitation();
  });
  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await createChild();
  });

  return {refresh, reset};
}
