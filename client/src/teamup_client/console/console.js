import {taskPanel} from "/console-tasks.js";

const get = (id) => document.getElementById(id);
const svg = (name) => document.createElementNS("http://www.w3.org/2000/svg", name);
const stage = get("stage");
const state = {
  workspace: {sessions: [], rooms: []},
  page: "workspace",
  focused: null,
  room: null,
  actor: null,
  chat: null,
  roomView: "chats",
  requestNumber: 0,
  roomRead: null,
  teamNumber: 0,
  connected: false,
};
const wireLimit = 20;
let key = window.location.hash.slice(1);
try {
  if (key) sessionStorage.setItem("teamup-console", key);
  else key = sessionStorage.getItem("teamup-console");
} catch {}
if (window.location.hash) history.replaceState(null, "", window.location.pathname);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function read(path, params, action) {
  const url = new URL(path, location.origin);
  for (const [name, value] of Object.entries(params || {})) {
    if (value !== null && value !== undefined) url.searchParams.set(name, value);
  }
  const response = await fetch(url, {
    method: action ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + key,
      ...(action?.body ? {"Content-Type": "application/json"} : {}),
    },
    body: action?.body ? JSON.stringify(action.body) : undefined,
    cache: "no-store",
    signal: AbortSignal.timeout(21000),
  });
  if (!response.ok && !action) throw new Error(response.status === 401 ? "access" : "connection");
  return await response.json();
}

function activeRoom() {
  return state.workspace.rooms.find((room) => room.id === state.room);
}

function sessionFor(id) {
  return state.workspace.sessions.find((session) => session.id === id);
}

function related(room) {
  return room.memberships.some((join) => join.session === state.focused);
}

const tasks = taskPanel({
  request: read,
  room: () => state.room,
  actor: () => sessionFor(state.actor),
  sessions: () => state.workspace.sessions,
  active: () => state.page === "room" && state.roomView === "tasks",
});

function setRoomView(view) {
  state.roomView = view;
  const isTasks = view === "tasks";
  get("tasks-tab").setAttribute("aria-pressed", String(isTasks));
  get("chats-tab").setAttribute("aria-pressed", String(!isTasks));
  get("actor-label").textContent = isTasks ? "Acting as" : "Viewing as";
  get("task-board").hidden = !isTasks;
  get("chat-scroll").hidden = isTasks;
  if (isTasks) tasks.refresh();
}

function card(label, description) {
  const button = node("button", undefined, "node");
  button.type = "button";
  const text = node("span");
  text.append(node("strong", label), node("small", description));
  button.append(text);
  return button;
}

function renderWorkspace() {
  const selected = document.activeElement;
  const oldSession = selected?.dataset.session;
  const oldRoom = selected?.dataset.room;
  const animate = state.page === "workspace" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  const positions = animate ? new Map(Array.from(get("rooms").querySelectorAll("button"), (button) =>
    [button.dataset.room, button.getBoundingClientRect().top])) : new Map();
  const sessions = state.workspace.sessions;
  const rooms = [...state.workspace.rooms];
  if (state.focused && state.page === "workspace") {
    rooms.sort((a, b) => Number(related(b)) - Number(related(a)));
  }
  get("sessions-title").textContent = "My sessions (" + sessions.length + ")";
  get("rooms-title").textContent = "Rooms (" + rooms.length + ")";
  const sessionButtons = sessions.map((session) => {
    const label = session.state === "ready" ? session.harness
      : session.harness + " · " + (session.state === "not_registered" ? "Unregistered" : "Unavailable");
    const button = card(session.label, label);
    button.dataset.session = session.id;
    button.setAttribute("aria-pressed", String(state.focused === session.id));
    button.classList.toggle("dimmed", Boolean(state.focused && state.focused !== session.id));
    return button;
  });
  const roomButtons = rooms.map((room) => {
    const noun = room.memberships.length === 1 ? "session" : "sessions";
    const button = card(room.name, room.memberships.length + " of your " + noun);
    button.dataset.room = room.id;
    if (state.page === "room" && state.room === room.id) button.setAttribute("aria-current", "page");
    button.classList.toggle("related", Boolean(state.focused && state.page === "workspace" && related(room)));
    button.classList.toggle("dimmed", Boolean(state.focused && state.page === "workspace" && !related(room)));
    return button;
  });
  get("sessions").replaceChildren(...(sessionButtons.length ? sessionButtons : [node("p", "No approved sessions", "empty")]));
  get("rooms").replaceChildren(...(roomButtons.length ? roomButtons : [node("p", "Your joined rooms appear here", "empty")]));
  const focus = sessionButtons.find((button) => button.dataset.session === oldSession) ||
    roomButtons.find((button) => button.dataset.room === oldRoom);
  focus?.focus({preventScroll:true});
  const moved = roomButtons.filter((button) => {
    const before = positions.get(button.dataset.room);
    if (before === undefined) return false;
    const delta = before - button.getBoundingClientRect().top;
    if (Math.abs(delta) < 1) return false;
    button.animate([{transform:"translateY(" + delta + "px)"}, {transform:"translateY(0)"}],
      {duration:320, easing:"cubic-bezier(.22,.74,.21,1)"});
    return true;
  });
  if (moved.length) {
    const until = performance.now() + 335;
    const paint = (now) => {
      drawWires();
      if (now < until) requestAnimationFrame(paint);
    };
    requestAnimationFrame(paint);
  } else drawWires();
}

function drawWires() {
  const layer = get("links");
  layer.replaceChildren();
  if (state.page !== "workspace") return;
  const count = state.workspace.rooms.reduce((total, room) => total + room.memberships.length, 0);
  if (count > wireLimit) return;

  const from = new Map(Array.from(get("sessions").querySelectorAll("button"), (button) =>
    [button.dataset.session, button.getBoundingClientRect()]));
  const to = new Map(Array.from(get("rooms").querySelectorAll("button"), (button) =>
    [button.dataset.room, button.getBoundingClientRect()]));
  const origin = stage.getBoundingClientRect();
  const sessions = get("session-scroll").getBoundingClientRect();
  const rooms = get("room-scroll").getBoundingClientRect();
  for (const room of state.workspace.rooms) {
    const end = to.get(room.id);
    if (!end) continue;
    const endY = end.top + end.height / 2;
    if (endY < rooms.top || endY > rooms.bottom) continue;
    for (const membership of room.memberships) {
      if (state.focused && membership.session !== state.focused) continue;
      const start = from.get(membership.session);
      if (!start) continue;
      const startY = start.top + start.height / 2;
      if (startY < sessions.top || startY > sessions.bottom) continue;
      const x1 = start.right - origin.left;
      const x2 = end.left - origin.left;
      const y1 = startY - origin.top;
      const y2 = endY - origin.top;
      const bend = Math.max(6, (x2 - x1) * .46);
      const path = svg("path");
      path.dataset.focused = String(Boolean(state.focused));
      path.dataset.active = String(membership.active_task === true);
      path.setAttribute("d", "M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 +
        " " + (x2 - bend) + " " + y2 + " " + x2 + " " + y2);
      layer.append(path);
    }
  }
}

function show(page) {
  state.page = page;
  stage.dataset.page = page;
  get("workspace-nav").setAttribute("aria-pressed", String(page !== "team"));
  get("team-nav").setAttribute("aria-pressed", String(page === "team"));
  get("detail").inert = page !== "room";
  get("session-scroll").inert = page !== "workspace";
  get("room-scroll").inert = page === "team";
  get("rooms-title").hidden = page === "room";
  get("back").hidden = page !== "room";
  get("team-panel").hidden = page !== "team";
  renderWorkspace();
  if (page !== "room") tasks.reset();
  if (page === "team") refreshTeam();
  if (page === "workspace") setTimeout(drawWires, 450);
}

function renderRoomHeader() {
  const room = activeRoom();
  if (!room) return;
  get("compact-room").textContent = room.name;
  get("room-id").value = room.id;
  get("detail").setAttribute("aria-label", room.name);
  const chooser = get("chat-session");
  const options = room.memberships.map(({session}) => {
    const option = node("option", sessionFor(session)?.label || session);
    option.value = session;
    option.selected = state.actor === session;
    return option;
  });
  chooser.replaceChildren(...options);
  const members = room.memberships.map(({session}) => node("span", sessionFor(session)?.label || session));
  get("roster").replaceChildren(...members);
  const membership = room.memberships.find((item) => item.session === state.actor);
  get("board-status").textContent = membership
    ? "Version " + membership.current + " · " + (membership.acknowledged === membership.current ? "Acknowledged" : "Acknowledgment needed")
    : "";
}

function openRoom(id) {
  if (state.room === id && state.page === "room") return;
  const room = state.workspace.rooms.find((item) => item.id === id);
  if (!room) return;
  tasks.reset();
  state.room = id;
  state.actor = room.memberships.find((join) => join.session === state.focused)?.session ?? room.memberships[0].session;
  state.chat = null;
  show("room");
  renderRoomHeader();
  renderChat();
  setRoomView(state.roomView);
  get("back").focus({preventScroll:true});
  refreshRoom();
}

const messagesForState = {
  board_ack_required: "This session must read and acknowledge the room board before its chat is available.",
  not_registered: "This session needs to register before it can read chat.",
  not_found: "This room is unavailable to the selected session.",
  unauthorized: "The selected session no longer has access.",
  unavailable: "Chat is temporarily unavailable.",
};

function renderChat(options = {}) {
  const chat = state.chat;
  const page = get("chat-scroll");
  const prior = page.scrollTop;
  const priorHeight = page.scrollHeight;
  const nearEnd = priorHeight - prior - page.clientHeight < 30;
  get("older").hidden = !chat?.older;
  get("chat-status").textContent = !chat ? "Reading room messages…"
    : messagesForState[chat.state] || (chat.messages?.length ? "" : "No messages in this session’s history.");
  const rows = (chat?.messages || []).map((message) => {
    const date = message.created_at ? new Date(message.created_at) : null;
    const time = node("time", date && !Number.isNaN(date.getTime())
      ? date.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : "—");
    if (date && !Number.isNaN(date.getTime())) {
      time.dateTime = message.created_at;
      time.title = date.toLocaleString();
    }
    const row = node("article", undefined, "message");
    const content = node("div");
    content.append(node("strong", message.sender), node("p", message.text));
    row.append(time, content);
    return row;
  });
  get("messages").replaceChildren(...rows);
  if (options.prepend) page.scrollTop = prior + (page.scrollHeight - priorHeight);
  else if (options.reset || nearEnd) page.scrollTop = page.scrollHeight;
  else page.scrollTop = prior;
}

async function refreshRoom(before) {
  if (state.page !== "room") return;
  const request = ++state.requestNumber;
  const identity = state.actor;
  const room = state.room;
  const observedLatest = activeRoom()?.latest_message_id;
  const previous = state.chat;
  state.roomRead = {room, identity, request};
  const after = !before && previous?.state === "ready" ? previous.messages.at(-1)?.id ?? "0" : null;
  const valid = () => request === state.requestNumber && room === state.room &&
    identity === state.actor && state.page === "room";
  try {
    const response = await read("/api/room", {session: identity, room, before, after});
    if (!valid()) return;
    if (response.state === "ready" && previous?.state === "ready") {
      const all = new Map(previous.messages.map((message) => [message.id, message]));
      for (const message of response.messages) all.set(message.id, message);
      response.messages = Array.from(all.values()).sort((a, b) =>
        a.id.length - b.id.length || a.id.localeCompare(b.id));
      if (after !== null) response.older = previous.older;
      if (before) response.more_recent = previous.more_recent;
    }
    response.observedLatest = before ? previous?.observedLatest : observedLatest;
    state.chat = response;
    renderChat({prepend: Boolean(before), reset: !previous});
  } catch {
    if (valid()) {
      state.chat = {state:"unavailable", messages:[]};
      renderChat();
    }
  } finally {
    if (state.roomRead?.request === request) state.roomRead = null;
  }
}

async function refreshTeam() {
  const request = ++state.teamNumber;
  const preferred = sessionFor(state.focused);
  const actor = preferred?.state === "ready" ? preferred
    : state.workspace.sessions.find((item) => item.state === "ready");
  if (!actor) {
    get("team-list").replaceChildren();
    get("team-status").textContent = "An approved, registered session can view the team.";
    return;
  }
  if (!get("team-list").children.length) get("team-status").textContent = "Reading team members…";
  try {
    const result = await read("/api/team", {session: actor.id});
    if (request !== state.teamNumber || state.page !== "team") return;
    get("team-status").textContent = result.state === "ready"
      ? "Viewing as " + actor.label
      : "Team is unavailable for this session.";
    get("team-list").replaceChildren(...result.members.map((member) => node("li", member.role)));
  } catch {
    if (request === state.teamNumber) get("team-status").textContent = "Team is temporarily unavailable.";
  }
}

function setConnection(message, live) {
  get("connection").textContent = message;
  get("connection").dataset.live = String(live);
}

async function refresh() {
  let expire = false;
  try {
    const snapshot = await read("/api/workspace");
    const changed = JSON.stringify(snapshot) !== JSON.stringify(state.workspace);
    const oldBoard = activeRoom()?.memberships.find((item) => item.session === state.actor);
    state.workspace = snapshot;
    if (!sessionFor(state.focused)) state.focused = null;
    const active = activeRoom();
    setConnection(snapshot.sessions.some((session) => session.state === "ready") ? "Live" : "Client ready",
      snapshot.sessions.some((session) => session.state === "ready"));
    if (state.page === "room" && !active) {
      state.chat = null;
      state.room = null;
      show("workspace");
    } else if (state.page === "room" && !active.memberships.some((item) => item.session === state.actor)) {
      state.actor = active.memberships[0].session;
      state.chat = null;
      renderChat();
      refreshRoom();
    } else if (state.page === "room") {
      const board = active.memberships.find((item) => item.session === state.actor);
      const boardChanged = board.current !== oldBoard?.current || board.acknowledged !== oldBoard?.acknowledged;
      if (boardChanged) {
        state.chat = null;
        renderChat();
      }
      const readingCurrentRoom = state.roomRead?.room === state.room &&
        state.roomRead?.identity === state.actor;
      const behind = active.latest_message_id !== state.chat?.observedLatest || state.chat?.more_recent;
      if (boardChanged || (!readingCurrentRoom && (behind || state.chat?.state === "unavailable"))) {
        refreshRoom();
      }
    }
    if (changed) {
      renderWorkspace();
      if (state.page === "room") renderRoomHeader();
    }
    if (state.page === "team") refreshTeam();
    if (state.page === "room" && state.roomView === "tasks") tasks.refresh();
  } catch (error) {
    expire = error.message === "access";
    setConnection(expire ? "Open a fresh console link" : "Reconnecting…", false);
    state.workspace = {sessions:[], rooms:[]};
    state.chat = null;
    state.room = null;
    show("workspace");
  } finally {
    if (!expire) setTimeout(refresh, 5000);
  }
}

let copyFeedback;
async function copyId() {
  const field = get("room-id");
  const tip = get("copy-tip");
  const control = get("copy-room").parentElement;
  window.clearTimeout(copyFeedback);
  control.dataset.feedback = "";
  tip.textContent = "Copying…";
  let copied = false;
  try {
    await navigator.clipboard.writeText(field.value);
    copied = true;
  } catch {
    field.focus({preventScroll:true});
    field.select();
    try { copied = document.execCommand("copy"); } catch {}
  }
  tip.textContent = copied ? "Copied" : "ID selected";
  get("copy-status").textContent = copied ? "Room ID copied" : "Room ID selected. Use your copy shortcut.";
  copyFeedback = setTimeout(() => {
    delete control.dataset.feedback;
    tip.textContent = "Copy room ID";
  }, 1450);
}

get("workspace-nav").addEventListener("click", () => show("workspace"));
get("team-nav").addEventListener("click", () => show("team"));
get("back").addEventListener("click", () => show("workspace"));
get("sessions").addEventListener("click", (event) => {
  const clicked = event.target.closest("[data-session]");
  if (!clicked) return;
  state.focused = state.focused === clicked.dataset.session ? null : clicked.dataset.session;
  renderWorkspace();
});
get("rooms").addEventListener("click", (event) => {
  const clicked = event.target.closest("[data-room]");
  if (clicked) openRoom(clicked.dataset.room);
});
get("tasks-tab").addEventListener("click", () => setRoomView("tasks"));
get("chats-tab").addEventListener("click", () => setRoomView("chats"));
get("chat-session").addEventListener("change", (event) => {
  state.actor = event.target.value;
  state.chat = null;
  renderRoomHeader();
  renderChat();
  tasks.refresh({force: true});
  refreshRoom();
});
get("copy-room").addEventListener("click", copyId);
get("older").addEventListener("click", () => {
  if (state.chat?.older) refreshRoom(state.chat.older);
});
for (const area of ["session-scroll", "room-scroll"]) {
  get(area).addEventListener("scroll", drawWires, {passive:true});
}
window.addEventListener("resize", drawWires);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.page !== "workspace") show("workspace");
});
if (key) refresh();
else {
  setConnection("Open the URL printed by TeamUp Client", false);
  renderWorkspace();
}
