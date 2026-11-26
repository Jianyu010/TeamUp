import {messageRow, readTranscript, sameBoard} from "/console-history.mjs";
import {roomBoard} from "/console-board.js";
import {memberNames, roomMembers, renderSessionChoices, renderTeam} from "/console-members.js";
import {taskPanel} from "/console-tasks.js";
import {sessionControls} from "/console-sessions.js";

const get = (id) => document.getElementById(id);
const svg = (name) => document.createElementNS("http://www.w3.org/2000/svg", name);
const stage = get("stage");
const state = {
  workspace: {sessions: [], rooms: []},
  page: "workspace",
  focused: null,
  room: null,
  reader: null,
  teamReader: null,
  teamMember: null,
  teamPending: false,
  actor: null,
  chat: null,
  roomView: "chats",
  requestNumber: 0,
  roomRead: null,
  teamNumber: 0,
  connected: false,
};
const wireLimit = 20;
const refreshInterval = 5000;
const desktop = location.protocol === "teamup:";
let key;
if (!desktop) {
  key = window.location.hash.slice(1);
  try {
    if (key) sessionStorage.setItem("teamup-console", key);
    else key = sessionStorage.getItem("teamup-console");
  } catch {}
  if (window.location.hash) history.replaceState(null, "", window.location.pathname);
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function read(path, params, action) {
  const url = new URL(path, location.href);
  for (const [name, value] of Object.entries(params || {})) {
    if (value !== null && value !== undefined) url.searchParams.set(name, value);
  }
  const response = await fetch(url, {
    method: action ? "POST" : "GET",
    headers: {
      ...(key ? {Authorization: "Bearer " + key} : {}),
      ...(action?.body ? {"Content-Type": "application/json"} : {}),
    },
    body: action?.body ? JSON.stringify(action.body) : undefined,
    cache: "no-store",
    signal: AbortSignal.timeout(action?.timeout ?? 21000),
  });
  if (!response.ok && !action) {
    if (response.status === 409 && (await response.json()).state === "sign_in_required") {
      throw new Error("sign_in_required");
    }
    throw new Error(response.status === 401 ? "access" : "connection");
  }
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

function readyMemberships(room) {
  return room.memberships.filter((item) => sessionFor(item.session)?.state === "ready");
}

function acknowledged(item) {
  return item.current === item.acknowledged;
}

function preferredSession(memberships) {
  return (memberships.find((item) => item.session === state.focused) || memberships[0])?.session ?? null;
}

function selectRoomSubjects(room) {
  const joined = readyMemberships(room);
  const eligible = joined.filter(acknowledged);
  if (!joined.some((item) => item.session === state.reader)) {
    state.reader = preferredSession(eligible.length ? eligible : joined);
  }
  if (!eligible.some((item) => item.session === state.actor)) state.actor = preferredSession(eligible);
}

const boardPanel = roomBoard({get, request: read});

const tasks = taskPanel({
  request: read,
  room: () => state.room,
  actor: () => sessionFor(state.actor),
  sessions: () => state.workspace.sessions,
  active: () => state.page === "room" && Boolean(state.reader) && state.roomView === "tasks",
});

const lifecycle = sessionControls({
  request: read,
  changed(session, room, result, membershipId) {
    if (room?.id === state.room) members.reset({preserveNames: true});
    const snapshot = state.workspace;
    const retired = snapshot.retired_sessions ?? [];
    const sessions = room ? snapshot.sessions : snapshot.sessions.filter((item) => item.id !== session.id);
    const directory = snapshot.directory_available && sessions.some((item) => item.state === "ready");
    const rooms = snapshot.rooms.flatMap((item) => {
      if (room && item.id !== room.id) return [item];
      const memberships = item.memberships.filter((link) => link.session !== session.id ||
        (membershipId && link.membership_id !== membershipId));
      if (memberships.length) return [{...item, memberships}];
      if (directory && item.history_policy) return [{...item, memberships, latest_message_id: null}];
      return [];
    });
    applyWorkspace({
      ...snapshot,
      sessions,
      rooms,
      directory_available: directory,
      retired_sessions: room || retired.some((item) => item.id === session.id) ? retired
        : [...retired, {...session, role_description: result.role_description}],
    });
    return refresh();
  },
});

const members = roomMembers({
  read,
  subject: () => state.page === "room" && state.reader ? {room: state.room, session: state.reader} : null,
  chat: () => state.chat,
  policyChanged: renderHistoryBoundary,
  namesChanged: tasks.updateMemberNames,
  actionFor: rosterAction,
});

function rosterAction(member) {
  const room = activeRoom();
  if (!room) return;
  const joined = readyMemberships(room);
  const local = joined.find(({session}) => sessionFor(session)?.member_id === member.id);
  if (local?.membership_id && (!member.membership_id || member.membership_id === local.membership_id)) {
    const session = sessionFor(local.session);
    return {kind: "leave", run: () => lifecycle.leave(session, room, local.membership_id)};
  }
  const creator = joined.find(({can_remove_members}) => can_remove_members === true);
  if (creator && !local && member.membership_id) {
    return {kind: "remove", run: () => removeRoomMember(room, creator.session, member)};
  }
}

async function removeRoomMember(room, session, member) {
  if (!window.confirm("Remove " + member.role + " from this room?")) return;
  const status = get("session-action-status");
  try {
    const result = await read(
      "/api/room-members/remove", {room: room.id, session, member: member.id},
      {body: {membership_id: member.membership_id}},
    );
    if (result.state === "ready") {
      status.textContent = member.role + (result.departure_event_id === null
        ? " had already left the room." : " was removed from the room.");
      if (state.page === "room" && state.room === room.id) {
        members.reset({preserveNames: true});
        members.refresh();
      }
    } else {
      status.textContent = result.state === "membership_changed"
        ? "The member rejoined. Check the roster before trying again."
        : "TeamUp could not remove this member.";
    }
  } catch {
    status.textContent = "TeamUp could not confirm the removal.";
  }
  await refresh();
}

function renderRoomView() {
  const joined = Boolean(state.reader);
  const isTasks = state.roomView === "tasks";
  get("detail").toggleAttribute("data-preview", !joined);
  get("room-preview").hidden = joined;
  get("chat-heading").hidden = !joined;
  get("member-rail").hidden = !joined;
  get("tasks-tab").setAttribute("aria-pressed", String(isTasks));
  get("chats-tab").setAttribute("aria-pressed", String(!isTasks));
  get("task-board").hidden = !joined || !isTasks;
  get("chat-scroll").hidden = !joined || isTasks;
}

function setRoomView(view) {
  state.roomView = view;
  renderRoomView();
  if (view === "tasks") tasks.refresh();
}

function card(label, description) {
  const button = node("button", undefined, "node");
  button.type = "button";
  const text = node("span");
  text.append(typeof label === "string" ? node("strong", label) : label, node("small", description));
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
  lifecycle.workspace(sessionFor(state.focused), state.workspace.retired_sessions ?? []);
  const names = memberNames(sessions.map(({id, member_id, label}) => ({
    id: member_id ?? id, name: label,
  })), "strong");
  const sessionButtons = sessions.map((session, index) => {
    const label = session.state === "ready" ? session.harness
      : session.harness + " · " + (session.state === "not_registered" ? "Unregistered" : "Unavailable");
    const name = names[index];
    name.view.title = name.label;
    const button = card(name.view, label);
    button.classList.toggle("session-ready", session.state === "ready");
    if (session.owner_username) {
      const owner = "by " + session.owner_username;
      const detail = button.firstElementChild.lastElementChild;
      if (session.state === "ready") detail.textContent = owner + " · " + label;
      else button.firstElementChild.insertBefore(node("small", owner, "member-owner"), detail);
    }
    button.dataset.session = session.id;
    button.setAttribute("aria-pressed", String(state.focused === session.id));
    button.classList.toggle("dimmed", Boolean(state.focused && state.focused !== session.id));
    return button;
  });
  const roomButtons = rooms.map((room) => {
    const noun = room.memberships.length === 1 ? "session" : "sessions";
    const description = room.memberships.length
      ? room.memberships.length + " of your " + noun : "No local sessions joined";
    const button = card(room.name, description);
    button.dataset.room = room.id;
    if (state.page === "room" && state.room === room.id) button.setAttribute("aria-current", "page");
    button.classList.toggle("related", Boolean(state.focused && state.page === "workspace" && related(room)));
    button.classList.toggle("dimmed", Boolean(state.focused && state.page === "workspace" && !related(room)));
    return button;
  });
  get("sessions").replaceChildren(...(sessionButtons.length ? sessionButtons : [node("p", "No approved sessions", "empty")]));
  const roomRows = [...roomButtons];
  if (!state.workspace.directory_available && sessions.some((session) => session.state === "ready")) {
    const status = node("p", "Room directory unavailable", "empty");
    status.setAttribute("role", "status");
    roomRows.unshift(status);
  }
  if (!roomRows.length) {
    roomRows.push(node("p", state.workspace.directory_available ? "No team rooms" : "No rooms available", "empty"));
  }
  get("rooms").replaceChildren(...roomRows);
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
  if (page !== "room") {
    state.requestNumber++;
    state.roomRead = null;
    state.chat = null;
    state.reader = null;
    state.room = null;
    boardPanel.setRoom(null, [], null);
    members.reset();
    renderChat();
  }
  if (page !== "team") {
    state.teamNumber++;
    state.teamPending = false;
    get("team-list").replaceChildren();
  }
  state.page = page;
  stage.dataset.page = page;
  get("workspace-nav").setAttribute("aria-pressed", String(page !== "team"));
  get("team-nav").setAttribute("aria-pressed", String(page === "team"));
  get("detail").inert = page !== "room";
  get("sessions-title").closest("section").inert = page !== "workspace";
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
  const chooser = get("task-actor");
  const actors = readyMemberships(room).filter(acknowledged).map(({session}) => sessionFor(session));
  chooser.disabled = actors.length === 0;
  renderSessionChoices(chooser, actors, state.actor, "No acknowledged session");
  const membership = room.memberships.find((item) => item.session === state.reader);
  get("room-heading").textContent = room.name;
  get("room-heading").hidden = Boolean(membership);
  const boardSessions = readyMemberships(room).map((entry) => ({
    ...sessionFor(entry.session),
    current: entry.current,
    acknowledged: entry.acknowledged,
    membership_id: entry.membership_id,
  }));
  boardPanel.setRoom(room.id, boardSessions, state.reader);
  const history = room.history_policy === "retained"
    ? "Retained messages can be read after joining and acknowledging the board."
    : "Room history starts when you join.";
  get("room-preview").textContent = membership ? "" : "Copy the room ID to join through your agent. " + history;
  renderRoomView();
}

function openRoom(id) {
  if (state.room === id && state.page === "room") return;
  const room = state.workspace.rooms.find((item) => item.id === id);
  if (!room) return;
  tasks.reset();
  state.room = id;
  state.reader = null;
  state.actor = null;
  selectRoomSubjects(room);
  state.chat = null;
  members.reset();
  show("room");
  renderRoomHeader();
  renderChat();
  if (state.roomView === "tasks") tasks.refresh();
  get("back").focus({preventScroll:true});
  refreshRoom();
  members.refresh();
}

const messagesForState = {
  board_ack_required: "This session must read and acknowledge the room board before its chat is available.",
  not_registered: "This session needs to register before it can read chat.",
  not_found: "This room is unavailable to the selected session.",
  membership_changed: "Room membership changed. Refreshing history…",
  unauthorized: "The selected session no longer has access.",
  unavailable: "Chat is temporarily unavailable.",
};

function renderHistoryBoundary() {
  const chat = state.chat;
  const policy = members.historyPolicy() || chat?.history_policy;
  const boundary = policy === "retained" ? "Includes retained messages from before joining."
    : policy === "since_joining" ? "History starts when this member joined the room."
    : "Room history follows this member’s access and the room’s history policy.";
  get("history-boundary").textContent = chat?.state === "ready"
    ? boundary + (chat.history_truncated ? " Some earlier messages have expired." : "") : "";
}

function renderChat(options = {}) {
  const chat = state.chat;
  const page = get("chat-scroll");
  const prior = page.scrollTop;
  const priorHeight = page.scrollHeight;
  const nearEnd = priorHeight - prior - page.clientHeight < 30;
  get("older").hidden = !chat?.older;
  get("chat-status").textContent = !chat ? "Reading room messages…"
    : messagesForState[chat.state] || (chat.messages?.length ? "" : "No messages in this session’s history.");
  renderHistoryBoundary();
  members.chatChanged();
  const list = get("messages");
  const focused = document.activeElement?.closest(".mention-overflow")?.dataset.id;
  const disclosureStates = new Map(Array.from(list.querySelectorAll(".mention-disclosure"), (wrapper) => {
    const details = wrapper.firstElementChild;
    return [details.dataset.id, {
      expanded: details.open,
      dismissHover: wrapper.matches("[data-dismiss-hover]:hover"),
      dismissFocus: wrapper.matches("[data-dismiss-focus]:focus-within"),
    }];
  }));
  const rows = (chat?.messages || []).map((message) => messageRow(node, message, disclosureStates.get(message.id)));
  list.replaceChildren(...rows);
  if (focused) {
    const details = Array.from(list.querySelectorAll(".mention-overflow")).find((item) => item.dataset.id === focused);
    details?.querySelector("summary").focus({preventScroll: true});
  }
  if (options.prepend) page.scrollTop = prior + (page.scrollHeight - priorHeight);
  else if (options.reset || nearEnd) page.scrollTop = page.scrollHeight;
  else page.scrollTop = prior;
}

async function refreshRoom(before) {
  if (state.page !== "room" || !state.reader) return;
  const request = ++state.requestNumber;
  const session = state.reader;
  const room = state.room;
  const previous = state.chat;
  const valid = () => request === state.requestNumber && room === state.room &&
    session === state.reader && state.page === "room";
  state.roomRead = {request};
  try {
    const {page, reset} = await readTranscript(previous, before, async (cursor) => {
      const result = await read("/api/room", {session, room, ...cursor});
      if (!valid()) throw new Error("Room read superseded");
      return result;
    });
    if (!valid()) return;
    state.chat = page;
    if (JSON.stringify(previous) !== JSON.stringify(page)) {
      renderChat({prepend: Boolean(before) && !reset, reset});
    }
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
  const preferred = sessionFor(state.focused);
  const current = sessionFor(state.teamReader);
  const actor = current?.state === "ready" ? current : preferred?.state === "ready" ? preferred
    : state.workspace.sessions.find((item) => item.state === "ready");
  const changed = actor?.id !== state.teamReader || actor?.member_id !== state.teamMember;
  if (!changed && state.teamPending) return;
  const request = ++state.teamNumber;
  if (changed) get("team-list").replaceChildren();
  state.teamReader = actor?.id;
  state.teamMember = actor?.member_id;
  state.teamPending = Boolean(actor);
  if (!actor) {
    get("team-list").replaceChildren();
    get("team-status").textContent = "An approved, registered session can view the team.";
    return;
  }
  if (!get("team-list").children.length) get("team-status").textContent = "Reading team members…";
  try {
    const started = performance.now();
    const result = await read("/api/team", {session: actor.id});
    if (request !== state.teamNumber || state.page !== "team") return;
    get("team-status").textContent = result.state === "ready"
      ? ""
      : "Team is unavailable for this session.";
    renderTeam(result, started);
  } catch {
    if (request === state.teamNumber) {
      get("team-list").replaceChildren();
      get("team-status").textContent = "Team is temporarily unavailable.";
    }
  } finally {
    if (request === state.teamNumber) state.teamPending = false;
  }
}

function setConnection(message, live) {
  get("connection").textContent = message;
  get("connection").dataset.live = String(live);
}

let versionsTimer;

async function refreshVersions() {
  clearTimeout(versionsTimer);
  try {
    const result = await read("/api/versions");
    get("client-version").textContent = result.client_version ?? "Unknown";
    get("server-version").textContent = result.server_version ?? (
      result.server_state === "unavailable" ? "Unavailable" : "Unknown"
    );
  } catch (error) {
    get("client-version").textContent = "Unknown";
    get("server-version").textContent = "Unavailable";
    if (error.message === "access") return;
  }
  if (!stage.hidden) versionsTimer = setTimeout(refreshVersions, refreshInterval);
}

function applyWorkspace(snapshot) {
  const changed = JSON.stringify(snapshot) !== JSON.stringify(state.workspace);
  const oldReader = state.reader;
  const oldMember = sessionFor(oldReader)?.member_id;
  const oldBoard = activeRoom()?.memberships.find((item) => item.session === oldReader);
  state.workspace = snapshot;
  if (!sessionFor(state.focused)) state.focused = null;
  const active = activeRoom();
  setConnection(snapshot.sessions.some((session) => session.state === "ready") ? "Live" : "Client ready",
    snapshot.sessions.some((session) => session.state === "ready"));
  if (state.page === "room" && !active) {
    state.chat = null;
    state.room = null;
    show("workspace");
  } else if (state.page === "room") {
    const oldActor = state.actor;
    selectRoomSubjects(active);
    if (oldReader && !state.reader) tasks.reset();
    else if (state.actor !== oldActor) tasks.changeActor();
    const board = active.memberships.find((item) => item.session === state.reader);
    const scopeChanged = state.reader !== oldReader || sessionFor(state.reader)?.member_id !== oldMember ||
      board?.membership_id !== oldBoard?.membership_id;
    if (scopeChanged || !sameBoard(board, oldBoard)) {
      state.requestNumber++;
      state.chat = null;
      state.roomRead = null;
      members.reset({preserveNames: !scopeChanged});
      renderChat();
    }
    if (!state.roomRead) refreshRoom();
    members.refresh();
  }
  if (changed) {
    renderWorkspace();
    if (state.page === "room") renderRoomHeader();
  }
  if (state.page === "team") refreshTeam();
  if (state.page === "room") {
    boardPanel.refresh();
    if (state.roomView === "tasks") tasks.refresh();
  }
}

const signIn = get("sign-in");
const signInPassword = get("sign-in-password");
const rememberLogin = get("remember-login");
const wakeSettings = get("wake-settings");
const wakeOnMention = get("wake-on-mention");
const preferenceStatus = get("login-preference-status");
const rememberLoginLabel = get("remember-login-label");

async function loadRememberOption() {
  rememberLogin.checked = false;
  rememberLoginLabel.hidden = true;
  if (!desktop) return;
  try {
    const result = await read("/api/login");
    rememberLoginLabel.hidden = signIn.hidden || result.remember_available !== true;
  } catch {
    // Interactive sign-in is available while the preference check is retried.
  }
}

function showPreferenceStatus(text) {
  preferenceStatus.textContent = text;
  preferenceStatus.hidden = !text;
}

async function loadWakeSettings() {
  wakeSettings.hidden = true;
  try {
    const result = await read("/api/wake-settings");
    if (result.state === "ready" && !stage.hidden) {
      wakeOnMention.checked = result.enabled;
      wakeSettings.hidden = false;
    }
  } catch {
    // The Workspace can open with an older or temporarily disconnected Client.
  }
}

wakeOnMention.addEventListener("change", async () => {
  const enabled = wakeOnMention.checked;
  wakeOnMention.disabled = true;
  showPreferenceStatus("");
  try {
    const result = await read("/api/wake-settings", null, {body: {enabled}});
    if (result.state !== "ready") throw new Error("wake_settings");
    wakeOnMention.checked = result.enabled;
  } catch {
    wakeOnMention.checked = !enabled;
    showPreferenceStatus("TeamUp could not save the wake setting.");
  } finally {
    wakeOnMention.disabled = false;
  }
});
const loginMessages = {
  wrong_credentials: "Wrong username or password.",
  rate_limited: "Too many sign-in attempts. Try again later.",
  profile_unavailable: "TeamUp could not save the Client profile. Check the profile and try again.",
  unavailable: "TeamUp could not complete sign-in. Check the Server connection and try again.",
};

function displaySignIn() {
  const opening = signIn.hidden;
  signIn.hidden = false;
  stage.hidden = true;
  get("client-nav").hidden = true;
  get("versions").hidden = true;
  clearTimeout(versionsTimer);
  setConnection("Sign-in required", false);
  if (opening) {
    loadRememberOption();
    get("retire-dialog").close();
    get("sign-in-username").focus();
  }
}

function displayWorkspace() {
  if (!stage.hidden) return;
  signIn.hidden = true;
  signInPassword.value = "";
  get("sign-in-status").textContent = "";
  stage.hidden = false;
  get("client-nav").hidden = false;
  get("versions").hidden = false;
  loadWakeSettings();
  refreshVersions();
}

get("sign-in-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = get("sign-in-submit");
  const status = get("sign-in-status");
  if (submit.disabled) return;
  submit.disabled = true;
  status.textContent = "Signing in…";
  const pending = read("/api/login", null, {
    body: {
      username: get("sign-in-username").value,
      password: signInPassword.value,
      ...(desktop ? {remember: rememberLogin.checked} : {}),
    },
    timeout: 35000,
  });
  signInPassword.value = "";
  try {
    const result = await pending;
    if (result.state === "ready") {
      const keychainMessage = {
        save_failed: "TeamUp could not save credentials in Keychain.",
        forget_failed: "TeamUp could not remove saved credentials from Keychain.",
      };
      showPreferenceStatus(keychainMessage[result.keychain] ?? "");
      rememberLogin.checked = false;
      await refresh();
    } else if (Object.hasOwn(loginMessages, result.state)) {
      status.textContent = loginMessages[result.state];
    } else {
      status.textContent = loginMessages.unavailable;
    }
  } catch {
    status.textContent = loginMessages.unavailable;
  } finally {
    submit.disabled = false;
    if (!signIn.hidden) signInPassword.focus();
  }
});

let workspaceRequest = 0;
let workspaceTimer;

async function refresh() {
  clearTimeout(workspaceTimer);
  const request = ++workspaceRequest;
  let expire = false;
  try {
    const snapshot = await read("/api/workspace");
    if (request === workspaceRequest) {
      displayWorkspace();
      applyWorkspace(snapshot);
    }
  } catch (error) {
    if (request !== workspaceRequest) return;
    if (error.message === "sign_in_required") {
      state.workspace = {sessions: [], rooms: []};
      state.chat = null;
      state.room = null;
      state.focused = null;
      show("workspace");
      displaySignIn();
      return;
    }
    expire = error.message === "access";
    setConnection(expire ? "Open a fresh console link" : "Reconnecting…", false);
    state.workspace = {sessions:[], rooms:[]};
    state.chat = null;
    state.room = null;
    show("workspace");
  } finally {
    if (!expire && request === workspaceRequest) workspaceTimer = setTimeout(refresh, refreshInterval);
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
get("task-actor").addEventListener("change", (event) => {
  const room = activeRoom();
  if (state.roomView !== "tasks" || !room || !readyMemberships(room).some((item) =>
    item.session === event.target.value && acknowledged(item))) return;
  state.actor = event.target.value;
  tasks.changeActor();
});
get("copy-room").addEventListener("click", copyId);
get("older").addEventListener("click", () => {
  if (state.chat?.older) refreshRoom(state.chat.older);
});
for (const area of ["session-scroll", "room-scroll"]) {
  get(area).addEventListener("scroll", drawWires, {passive:true});
}
get("retired-sessions").addEventListener("toggle", drawWires);
window.addEventListener("resize", drawWires);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.page !== "workspace") show("workspace");
});
if (desktop || key) {
  refresh();
} else {
  setConnection("Open the URL printed by TeamUp Client", false);
  renderWorkspace();
}
