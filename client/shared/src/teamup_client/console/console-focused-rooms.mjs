const leadershipPath = "/api/room-leadership";
const reasons = {
  leader_profile: "Leadership role",
  same_owner: "Same account",
  recent_activity: "Recent room activity",
};

function localDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function isRoomLeader(room, session, membership) {
  if (!session || !membership) return false;
  return Number.isInteger(room.leadership_epoch)
    ? room.leader_member_id === session.member_id
    : membership.can_remove_members === true;
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
      get("leadership-room-name").textContent = room.name;
      dialog.showModal();
      await load(subject);
    },
    observe(workspace) {
      if (!pending || !workspace.sessions.some(({id, state}) => id === pending.session.id && state === "ready")) return;
      const room = workspace.rooms.find(({id}) => id === pending.room.id);
      if (!room?.memberships.some(({session}) => session === pending.session.id)) {
        footer.textContent = pending.session.label + " is no longer in " + pending.room.name + ".";
        pending = null;
      }
    },
  };
}
