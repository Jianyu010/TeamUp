const byId = (id) => document.getElementById(id);
const laneNames = ["Available", "Taken", "Ongoing"];
const errors = {
  board_ack_required: "Read and acknowledge the room board through your agent.",
  conflict: "The task changed. Its current state has been refreshed.",
  not_found: "This task operation is unavailable to the selected session.",
  not_registered: "Register the selected session first.",
  unauthorized: "This session is no longer authorized.",
  invalid_request: "Check the progress note and try again.",
  unavailable: "TeamUp could not confirm the operation. You can retry.",
};

function element(tag, text, css) {
  const item = document.createElement(tag);
  if (text) item.textContent = text;
  if (css) item.className = css;
  return item;
}

function button(label, handler, css) {
  const item = element("button", label, css);
  item.type = "button";
  item.addEventListener("click", handler);
  return item;
}

function time(stamp) {
  if (!stamp) return "";
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function openTaskLanes(items, actor) {
  const available = items.filter((task) => !task.participant_member_ids.length);
  const ongoing = items.filter((task) => task.participant_member_ids.length);
  const taken = actor
    ? ongoing.filter((task) => task.visible_to.includes(actor.id)
      && task.participant_member_ids.includes(actor.member_id))
    : [];
  return [["Available", available], ["Taken", taken], ["Ongoing", ongoing]];
}

export function taskPanel(context) {
  const lanes = byId("task-lanes");
  const status = byId("task-status");
  const more = byId("more-tasks");
  const archive = byId("finished-tasks");
  const tabs = byId("task-lane-nav");
  const expanded = new Set();
  const drafts = new Map();
  let memberNames = new Map();
  let selectedLane = "Available";
  let finished = false;
  let pages = 1;
  let sequence = 0;
  let signature = "";
  let busy = false;
  let pending = false;

  function reset() {
    sequence++;
    finished = false;
    pages = 1;
    signature = "";
    busy = false;
    pending = false;
    selectedLane = "Available";
    expanded.clear();
    drafts.clear();
    lanes.replaceChildren();
    status.textContent = "";
  }

  async function refresh({force = false} = {}) {
    if (!context.active() || (!force && (busy || pending || document.activeElement?.matches("#task-board textarea")))) return;
    const room = context.room();
    const view = finished ? "finished" : "open";
    const request = ++sequence;
    pending = true;
    let next = null;
    const items = [];
    let problem;
    try {
      for (let index = 0; index < pages; index++) {
        const result = await context.request("/api/tasks", {room, status: view, after: next});
        if (request !== sequence || room !== context.room()) return;
        if (result.state !== "ready") {
          problem = result.state;
          break;
        }
        items.push(...result.tasks);
        next = result.next_after;
        if (next === null) break;
      }
    } catch {
      problem = "unavailable";
    } finally {
      if (request === sequence) pending = false;
    }
    if (request !== sequence || room !== context.room() || !context.active()) return;
    if (problem) {
      lanes.replaceChildren();
      signature = "";
      more.hidden = true;
      status.textContent = problem === "board_ack_required"
        ? errors[problem] : "Room tasks are unavailable to your current sessions.";
      return;
    }
    const data = Array.from(new Map(items.map((task) => [task.task_id, task])).values());
    const actor = context.actor();
    const sessions = context.sessions();
    const sessionMembers = finished ? [] : sessions.map(({id, member_id}) => [id, member_id]);
    const key = JSON.stringify([finished, actor?.id, actor?.member_id, sessionMembers, data]);
    more.hidden = next === null;
    if (key === signature && !force) return;
    signature = key;
    if (!busy) status.textContent = "";
    render(data, sessions, actor);
  }

  function changeActor() {
    lanes.replaceChildren();
    signature = "";
    status.textContent = "";
    refresh({force: true});
  }

  function render(items, sessions, actor) {
    const oldScroll = new Map(Array.from(lanes.querySelectorAll(".task-list"), (list) =>
      [list.parentElement.dataset.lane, list.scrollTop]));
    const active = document.activeElement;
    const focusKey = active?.dataset.taskKey;
    archive.setAttribute("aria-pressed", String(finished));
    const groups = finished ? [["Finished", items]] : openTaskLanes(items, actor);
    tabs.hidden = finished;
    tabs.replaceChildren(...laneNames.map((name) => {
      const tab = button(name, () => {
        selectedLane = name;
        showLane();
      });
      tab.dataset.lane = name;
      return tab;
    }));
    lanes.dataset.finished = String(finished);
    lanes.replaceChildren(...groups.map(([name, tasks]) => {
      const lane = element("section", "", "task-lane");
      lane.dataset.lane = name;
      lane.setAttribute("aria-label", name + " tasks");
      const list = element("div", "", "task-list scroll-frame");
      if (name === "Available") {
        const assigned = [];
        const waiting = [];
        const unassigned = [];
        for (const task of tasks) {
          if (!task.addressed_member_ids.length) unassigned.push(task);
          else if (sessions.some(({id, member_id}) => task.visible_to.includes(id)
            && task.addressed_member_ids.includes(member_id))) assigned.push(task);
          else waiting.push(task);
        }
        for (const [label, rows] of [
          ["Assigned to your sessions", assigned],
          ["Waiting for addressed members", waiting],
          ["Unassigned", unassigned],
        ]) {
          if (rows.length) list.append(element("p", label, "task-group"), ...rows.map((task) => card(task, name)));
        }
      } else list.append(...tasks.map((task) => card(task, name)));
      if (!tasks.length) list.append(element("p", "No tasks", "muted"));
      lane.append(element("h3", name + " (" + tasks.length + ")"), list);
      return lane;
    }));
    showLane();
    lanes.querySelectorAll(".task-list").forEach((list) => {
      list.scrollTop = oldScroll.get(list.parentElement.dataset.lane) || 0;
    });
    if (focusKey) {
      Array.from(lanes.querySelectorAll("[data-task-key]"))
        .find((item) => item.dataset.taskKey === focusKey)?.focus({preventScroll: true});
    }
  }

  function showLane() {
    for (const lane of lanes.children) {
      lane.dataset.selected = String(finished || lane.dataset.lane === selectedLane);
    }
    for (const tab of tabs.children) tab.setAttribute("aria-pressed", String(tab.dataset.lane === selectedLane));
  }

  function memberLabel(id) {
    return memberNames.get(id)
      || context.sessions().find((session) => session.member_id === id)?.label;
  }

  function updateMemberName(name) {
    const id = name.dataset.taskMember;
    const label = memberLabel(id);
    const text = label || "Unknown member";
    if (text !== name.textContent) name.textContent = text;
    if (label) name.removeAttribute("title");
    else name.title = id;
  }

  function memberName(id) {
    const name = element("span");
    name.dataset.taskMember = id;
    updateMemberName(name);
    return name;
  }

  function appendNames(line, ids, prefix = "") {
    ids.forEach((id, index) => line.append(index ? ", " + prefix : prefix, memberName(id)));
  }

  function updateMemberNames(names) {
    memberNames = names;
    for (const name of lanes.querySelectorAll("[data-task-member]")) {
      updateMemberName(name);
    }
  }

  function card(task, lane) {
    const item = element("details", "", "task-card");
    const key = lane + ":" + task.task_id;
    item.open = expanded.has(key);
    item.addEventListener("toggle", () => {
      if (item.open) expanded.add(key);
      else expanded.delete(key);
    });
    const title = element("summary", task.assignment
      ? "Task #" + task.task_number + " · " + task.title
      : task.title);
    title.dataset.taskKey = key;
    item.append(title);
    if (task.stalled_since) item.append(element("p", "Stalled since " + time(task.stalled_since), "stalled"));
    if (task.addressed_member_ids.length) {
      const addressed = element("p", "", "muted");
      appendNames(addressed, task.addressed_member_ids, "@");
      item.append(addressed);
    }

    const body = element("div", "", "task-expanded");
    if (task.description) body.append(element("p", task.description));
    if (task.assignment) {
      const contact = element("p", "", "muted");
      contact.append(element("strong", "Progress contact"), " · ", memberName(task.creator_member_id));
      const eta = element("p", "", "muted");
      eta.append(element("strong", "ETA"), " · ", task.assignment.eta ?? "Assignee to propose");
      const criteria = element("p");
      criteria.append(element("strong", "Closing criteria"), " · ", task.assignment.closing_criteria);
      body.append(contact, eta, criteria);
    }
    const people = element("p", "", "muted");
    people.append(element("strong", "Participants"), " · ");
    if (task.participant_member_ids.length) appendNames(people, task.participant_member_ids);
    else people.append("None yet");
    body.append(people);
    if (task.last_progress_at) body.append(element("p", "Last progress · " + time(task.last_progress_at), "muted"));
    if (task.finished_at) body.append(element("p", "Finished · " + time(task.finished_at), "muted"));

    const actor = context.actor();
    const authorized = actor && task.visible_to.includes(actor.id);
    const participant = actor && task.participant_member_ids.includes(actor.member_id);
    const room = context.room();
    if (authorized && task.status === "open") {
      const actions = element("div", "", "task-actions");
      if (!participant && (!task.addressed_member_ids.length || task.addressed_member_ids.includes(actor.member_id))) {
        actions.append(button("Join task", () => change(task, actor, "join")));
      }
      if (participant) {
        const label = element("label", "Progress note");
        const entry = element("textarea");
        entry.maxLength = 8000;
        entry.rows = 2;
        const draftKey = [room, task.task_id, actor.id].join(":");
        entry.value = drafts.get(draftKey)?.note || "";
        entry.addEventListener("input", () => {
          const note = entry.value.trim();
          if (drafts.get(draftKey)?.note !== note) drafts.set(draftKey, {note, id: crypto.randomUUID()});
        });
        label.append(entry);
        body.append(label);
        actions.append(button("Report progress", () => {
          const draft = drafts.get(draftKey);
          if (!draft?.note) {
            status.textContent = "Enter a progress note.";
            entry.focus();
            return;
          }
          change(task, actor, "progress", {client_progress_id: draft.id, note: draft.note}, draftKey);
        }));
      }
      if (participant || task.creator_member_id === actor.member_id) {
        actions.append(button("Finish task", () => change(task, actor, "finish")));
      }
      if (actions.childElementCount) body.append(actions);
    }
    const historyActor = authorized ? actor.id : task.visible_to[0];
    if (historyActor) {
      const events = element("div", "", "task-events");
      body.append(button("History", async (event) => {
        event.currentTarget.remove();
        await history(task, historyActor, room, events);
      }), events);
    }
    item.append(body);
    return item;
  }

  async function history(task, session, room, region, after = null) {
    const loading = element("p", "Loading task history…", "muted");
    region.append(loading);
    try {
      const result = await context.request("/api/task-events", {room, session, task: task.task_id, after});
      if (!region.isConnected || room !== context.room()) return;
      loading.remove();
      if (result.state !== "ready") throw new Error();
      for (const entry of result.events) {
        const event = element("p", "", "muted");
        event.append(memberName(entry.actor_member_id), " · " + entry.kind + " · " +
          time(entry.created_at) + (entry.note ? " · " + entry.note : ""));
        region.append(event);
      }
      if (result.next_after) region.append(button("More history", async (click) => {
        click.currentTarget.remove();
        await history(task, session, room, region, result.next_after);
      }));
    } catch {
      loading.textContent = "Task history is unavailable.";
      if (region.isConnected && !loading.isConnected) region.append(loading);
    }
  }

  async function change(task, actor, action, body, draftKey) {
    const room = context.room();
    const current = () => context.active() && room === context.room() &&
      actor.id === context.actor()?.id && actor.member_id === context.actor()?.member_id;
    if (busy || !current() || task.room_id !== room) return;
    busy = true;
    status.textContent = "Saving…";
    try {
      const result = await context.request("/api/tasks/" + task.task_id + "/" + action, {room, session: actor.id}, {body});
      if (!current()) return;
      if (result.state === "ready") {
        if (draftKey) drafts.delete(draftKey);
        if (action === "join") selectedLane = "Ongoing";
        await refresh({force: true});
        if (!current()) return;
        status.textContent = {join: "Task joined.", progress: "Progress recorded.", finish: "Task finished."}[action];
      } else {
        if (result.state === "conflict") await refresh({force: true});
        if (!current()) return;
        status.textContent = errors[result.state] || errors.unavailable;
      }
    } catch {
      if (current()) status.textContent = errors.unavailable;
    } finally {
      busy = false;
    }
  }

  archive.addEventListener("click", () => {
    finished = !finished;
    pages = 1;
    lanes.replaceChildren();
    archive.setAttribute("aria-pressed", String(finished));
    refresh({force: true});
  });
  more.addEventListener("click", () => {
    pages++;
    refresh({force: true});
  });
  return {refresh, reset, changeActor, updateMemberNames};
}
