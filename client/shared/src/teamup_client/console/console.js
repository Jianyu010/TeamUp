import {messageRow, readTranscript, sameBoard} from "/console-history.mjs";
import {roomBoard} from "/console-board.js";
import {memberNames, roomMembers, renderSessionChoices, renderTeam} from "/console-members.js";
import {roomCommandError, taskPanel} from "/console-tasks.js";
import {sessionControls} from "/console-sessions.js";
import {sessionPage} from "/console-sessions.mjs";
import {focusedRoomPanel, isRoomLeader, leadershipDialog} from "/console-focused-rooms.mjs";

const get = (id) => document.getElementById(id);
const svg = (name) => document.createElementNS("http://www.w3.org/2000/svg", name);
const stage = get("stage");
const state = {
  workspace: {sessions: [], rooms: []},
  page: "workspace",
  focused: null,
  selectedRoom: null,
  sessionView: {all: false, query: "", page: 0},
  room: null,
  reader: null,
  teamReader: null,
  teamMember: null,
  teamPending: false,
  actor: null,
  focusActor: null,
  chat: null,
  roomView: "chats",
  requestNumber: 0,
  roomRead: null,
  teamNumber: 0,
  connected: false,
};
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

function preferredSession(memberships, preferred) {
  return (memberships.find((item) => item.session === preferred) || memberships[0])?.session ?? null;
}

function selectRoomSubjects(room, preferred = state.focused) {
  const joined = readyMemberships(room);
  const eligible = joined.filter(acknowledged);
  if (!joined.some((item) => item.session === state.reader)) {
    state.reader = preferredSession(eligible.length ? eligible : joined, preferred);
  }
  if (!eligible.some((item) => item.session === state.actor)) state.actor = preferredSession(eligible, preferred);
  if (!joined.some((item) => item.session === state.focusActor)) {
    state.focusActor = preferredSession(eligible.length ? eligible : joined, preferred);
  }
}

function roomLeader(room) {
  return (room && readyMemberships(room).find((item) =>
    isRoomLeader(room, sessionFor(item.session), item))) ?? null;
}

const boardPanel = roomBoard({get, request: read, roomCommandError});

const tasks = taskPanel({
  request: read,
  room: () => state.room,
  actor: () => sessionFor(state.actor),
  sessions: () => state.workspace.sessions,
  active: () => state.page === "room" && Boolean(state.reader) && state.roomView === "tasks",
});

function membershipEnded(session, room, result, membershipId) {
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
}

const lifecycle = sessionControls({
  request: read, changed: membershipEnded, roomCommandError, sessionFor, restoreSessionFocus,
});
const sessionMenu = get("session-menu");
const retireIdentity = get("retire-identity");
let sessionMenuTarget = null;

const leadership = leadershipDialog({
  get, node, request: read, departed: membershipEnded, refresh,
});

const focused = focusedRoomPanel({
  get, node, request: read,
  room: activeRoom,
  actor: () => sessionFor(state.focusActor),
  sessions: () => state.workspace.sessions,
  rooms: () => state.workspace.rooms,
  active: () => state.page === "room" && state.roomView === "focus",
  openRoom,
  changed: refresh,
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
  const local = readyMemberships(room)
    .find(({session}) => sessionFor(session)?.member_id === member.id);
  if (local?.membership_id && (!member.membership_id || member.membership_id === local.membership_id)) {
    const session = sessionFor(local.session);
    return {kind: "leave", run: () => isRoomLeader(room, session, local)
      ? leadership.open(session, room, local.membership_id)
      : lifecycle.leave(session, room, local.membership_id)};
  }
  const leader = roomLeader(room);
  if (leader && !local && member.membership_id) {
    return {kind: "remove", run: () => removeRoomMember(room, leader.session, member)};
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
      status.textContent = roomCommandError(result.state) ?? (result.state === "membership_changed"
        ? "The member rejoined. Check the roster before trying again."
        : "TeamUp could not remove this member.");
    }
  } catch {
    status.textContent = "TeamUp could not confirm the removal.";
  }
  await refresh();
}

const archiveDialog = get("room-archive-dialog");
const archiveSession = get("room-archive-session");
const archiveRoom = get("room-archive-room");
let archiveListRequest = 0;
let archiveDetailRequest = 0;
let archiveAfter = null;
let archiveBefore = null;

async function markGoalReached() {
  const room = activeRoom();
  const leader = roomLeader(room);
  if (!leader || !window.confirm("Mark the goal reached and archive this room?")) return;
  const button = get("archive-room");
  const status = get("session-action-status");
  button.disabled = true;
  try {
    const receipt = await read("/api/room-archive", {session: leader.session, room: room.id}, {});
    if (receipt.state === "ready") {
      status.textContent = room.name + " was archived.";
      show("workspace");
      await refresh();
    } else {
      status.textContent = receipt.state === "room_has_live_children"
        ? "Close the active child rooms before archiving " + room.name + "."
        : "TeamUp could not archive this room.";
    }
  } catch {
    status.textContent = "TeamUp could not confirm that the room was archived.";
  } finally {
    button.disabled = false;
  }
}

function openRoomArchive() {
  const sessions = state.workspace.sessions.filter((session) => session.state === "ready");
  const session = sessions.find((candidate) => candidate.id === state.focused) ?? sessions[0];
  renderSessionChoices(archiveSession, sessions, session?.id, "No available sessions");
  archiveDialog.showModal();
  loadArchiveRooms();
}

async function loadArchiveRooms(after = null) {
  const request = ++archiveListRequest;
  const status = get("room-archive-status");
  const more = get("room-archive-more");
  if (!after) {
    archiveDetailRequest += 1;
    archiveRoom.replaceChildren();
    get("room-archive-details").hidden = true;
  }
  more.hidden = true;
  if (!archiveSession.value) {
    status.textContent = "Select an available session to read its archived rooms.";
    return;
  }
  status.textContent = "Reading archived rooms…";
  try {
    const result = await read("/api/room-archive", {session: archiveSession.value, after});
    if (request !== archiveListRequest || !archiveDialog.open) return;
    if (result.state !== "ready") throw new Error();
    for (const room of result.rooms) {
      const reason = room.archive_reason === "goal_reached" ? "Goal reached" : "Inactive";
      const date = new Date(room.archived_at).toLocaleString();
      const option = node("option", room.name + " · " + reason + " · " + date);
      option.value = room.room_id;
      archiveRoom.append(option);
    }
    archiveAfter = result.next_after;
    more.hidden = !archiveAfter;
    status.textContent = archiveRoom.options.length ? "" : "No archived rooms for this session.";
    if (!after && archiveRoom.value) await loadArchivedRoom();
  } catch {
    if (request !== archiveListRequest || !archiveDialog.open) return;
    status.textContent = "TeamUp could not load archived rooms.";
    more.hidden = !after;
  }
}

async function loadArchivedRoom(before = null) {
  const request = ++archiveDetailRequest;
  const status = get("room-archive-status");
  const details = get("room-archive-details");
  const older = get("room-archive-older");
  if (!before) details.hidden = true;
  older.hidden = true;
  if (!archiveRoom.value) return;
  status.textContent = "Reading archived room…";
  try {
    const result = await read("/api/room-archive", {
      session: archiveSession.value, room: archiveRoom.value, before,
    });
    if (request !== archiveDetailRequest || !archiveDialog.open) return;
    if (result.state !== "ready") throw new Error();
    get("room-archive-members").replaceChildren(...result.members.map((member) => {
      const name = member.name ?? member.role_description ?? member.member_id;
      const description = member.description ? " — " + member.description : "";
      return node("li", name + description + " (" + member.member_id + ")");
    }));
    const history = get("room-archive-messages");
    const rows = result.messages.map((message) => messageRow(node, message));
    if (before) history.prepend(...rows);
    else history.replaceChildren(...rows);
    get("room-archive-history-status").textContent = result.history_state === "board_ack_required"
      ? "This session did not acknowledge the final Board version."
      : result.history_truncated ? "Some earlier messages expired before archiving."
        : !before && !rows.length ? "No messages in this session’s history." : "";
    archiveBefore = result.older;
    older.hidden = !archiveBefore;
    details.hidden = false;
    status.textContent = "";
  } catch {
    if (request !== archiveDetailRequest || !archiveDialog.open) return;
    status.textContent = "TeamUp could not load this archived room.";
    older.hidden = !before;
  }
}

function renderRoomView() {
  const joined = Boolean(state.reader);
  const view = state.roomView;
  get("detail").toggleAttribute("data-preview", !joined);
  get("room-preview").hidden = joined;
  get("chat-heading").hidden = !joined;
  get("member-rail").hidden = !joined;
  get("tasks-tab").setAttribute("aria-pressed", String(view === "tasks"));
  get("focus-tab").setAttribute("aria-pressed", String(view === "focus"));
  get("chats-tab").setAttribute("aria-pressed", String(view === "chats"));
  get("task-board").hidden = !joined || view !== "tasks";
  get("focus-board").hidden = !joined || view !== "focus";
  get("chat-scroll").hidden = !joined || view !== "chats";
}

function refreshRoomView() {
  if (state.roomView === "tasks") tasks.refresh();
  if (state.roomView === "focus") focused.refresh();
}

function setRoomView(view) {
  state.roomView = view;
  renderRoomView();
  refreshRoomView();
}

function card(label, description) {
  const button = node("button", undefined, "node");
  button.type = "button";
  const text = node("span");
  text.append(typeof label === "string" ? node("strong", label) : label, node("small", description));
  button.append(text);
  return button;
}

function restoreSessionFocus(sessionId) {
  if (state.page !== "workspace" || stage.hidden) return;
  const cards = get("sessions").querySelectorAll("button[data-session]");
  const card = Array.from(cards).find((button) => button.dataset.session === sessionId);
  (card ?? get("session-search")).focus({preventScroll:true});
}

function closeSessionMenu(restoreFocus = false) {
  if (!sessionMenuTarget) return;
  const {trigger} = sessionMenuTarget;
  sessionMenuTarget = null;
  sessionMenu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  wireLayout.unobserve(trigger);
  if (restoreFocus) trigger.focus({preventScroll:true});
}

function closeMovedSessionMenu() {
  if (!sessionMenuTarget) return;
  // A scroll event may arrive after its new position was measured at opening.
  const {trigger, bounds} = sessionMenuTarget;
  const {left, top, width, height} = trigger.getBoundingClientRect();
  if (left !== bounds.left || top !== bounds.top || width !== bounds.width || height !== bounds.height) {
    closeSessionMenu(true);
  }
}

function openSessionMenu(event) {
  const trigger = event.target.closest("button[data-session]");
  if (!trigger || state.page !== "workspace" || stage.hidden) return;
  const session = sessionFor(trigger.dataset.session);
  if (!lifecycle.canRetire(session)) return;
  event.preventDefault();
  closeSessionMenu();
  trigger.setAttribute("aria-expanded", "true");
  sessionMenu.hidden = false;
  retireIdentity.focus({preventScroll:true});
  const bounds = trigger.getBoundingClientRect();
  const atPointer = event.type === "contextmenu" && (event.clientX !== 0 || event.clientY !== 0);
  const x = atPointer ? event.clientX : bounds.left;
  const y = atPointer ? event.clientY : bounds.bottom;
  const {clientWidth, clientHeight} = document.documentElement;
  const inset = 6;
  sessionMenu.style.left = Math.max(inset, Math.min(x, clientWidth - sessionMenu.offsetWidth - inset)) + "px";
  sessionMenu.style.top = Math.max(inset, Math.min(y, clientHeight - sessionMenu.offsetHeight - inset)) + "px";
  sessionMenuTarget = {trigger, session, bounds};
  wireLayout.observe(trigger);
}

function renderWorkspaceSelection() {
  const workspace = state.page === "workspace";
  const room = workspace ? state.workspace.rooms.find(({id}) => id === state.selectedRoom) : null;
  const relatedSessions = new Set(room?.memberships.map(({session}) => session));
  const sessionFocused = workspace && Boolean(state.focused);
  const relatedRooms = new Set(sessionFocused ? state.workspace.rooms.filter(related).map(({id}) => id) : []);

  for (const button of get("sessions").querySelectorAll("button[data-session]")) {
    const id = button.dataset.session;
    const joined = relatedSessions.has(id);
    button.setAttribute("aria-pressed", String(state.focused === id));
    button.classList.toggle("related", joined);
    button.classList.toggle("dimmed", Boolean((state.focused && state.focused !== id) || (room && !joined)));
  }
  for (const button of get("rooms").querySelectorAll("button[data-room]")) {
    const id = button.dataset.room;
    const joined = relatedRooms.has(id);
    if (workspace) button.setAttribute("aria-pressed", String(room?.id === id));
    button.classList.toggle("related", joined);
    button.classList.toggle("dimmed", (sessionFocused && !joined) || Boolean(room && room.id !== id));
  }
}

function renderWorkspace() {
  const selected = sessionMenuTarget?.trigger ?? document.activeElement;
  closeSessionMenu();
  const oldSession = selected?.dataset.session;
  const oldRoom = selected?.dataset.room;
  const animate = state.page === "workspace" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  const positions = animate ? new Map(Array.from(get("rooms").querySelectorAll("button"), (button) =>
    [button.dataset.room, button.getBoundingClientRect().top])) : new Map();
  const sessions = state.workspace.sessions;
  const view = sessionPage(state.workspace, {...state.sessionView, focused: state.focused});
  state.sessionView.page = view.page;
  if (!view.sessions.some((session) => session.id === state.focused)) state.focused = null;
  const rooms = [...state.workspace.rooms];
  if (!rooms.some(({id}) => id === state.selectedRoom)) state.selectedRoom = null;
  if (state.focused && state.page === "workspace") {
    rooms.sort((a, b) => Number(related(b)) - Number(related(a)));
  }
  get("sessions-title").textContent = (state.sessionView.all ? "All sessions (" : "My sessions (") + view.count + ")";
  get("all-sessions").textContent = state.sessionView.all ? "My sessions (" + view.registered + ")"
    : "All sessions (" + view.total + ")";
  get("all-sessions").hidden = view.registered === view.total;
  get("all-sessions").setAttribute("aria-pressed", String(state.sessionView.all));
  get("session-pages").hidden = view.pages < 2;
  get("session-range").textContent = view.start + "–" + view.end + " of " + view.count;
  get("previous-sessions").disabled = view.page === 0;
  get("next-sessions").disabled = view.page === view.pages - 1;
  get("rooms-title").textContent = "Rooms (" + rooms.length + ")";
  const names = memberNames(sessions.map(({id, member_id, label}) => ({
    id: member_id ?? id, name: label,
  })), "strong");
  const namesBySession = new Map(sessions.map((session, index) => [session.id, names[index]]));
  const sessionButtons = view.sessions.map((session) => {
    const label = session.state === "ready" ? session.harness
      : session.harness + " · " + (session.state === "not_registered" ? "Unregistered" : "Unavailable");
    const name = namesBySession.get(session.id);
    name.view.title = name.label;
    const button = card(name.view, label);
    if (session.state === "ready") {
      button.classList.add("session-ready");
      button.dataset.label = name.label;
      button.setAttribute("aria-controls", sessionMenu.id);
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-keyshortcuts", "Shift+F10 ContextMenu");
    }
    if (session.owner_username) {
      const owner = "by " + session.owner_username;
      const detail = button.firstElementChild.lastElementChild;
      if (session.state === "ready") detail.textContent = owner + " · " + label;
      else button.firstElementChild.insertBefore(node("small", owner, "member-owner"), detail);
    }
    button.dataset.session = session.id;
    return button;
  });
  const roomButtons = rooms.map((room) => {
    const noun = room.memberships.length === 1 ? "session" : "sessions";
    const description = room.memberships.length
      ? room.memberships.length + " of your " + noun : "No local sessions joined";
    const button = card(room.name, description);
    button.dataset.room = room.id;
    if (state.page === "room" && state.room === room.id) button.setAttribute("aria-current", "page");
    if (state.page === "workspace") button.title = "Double-click or press Enter to open";
    return button;
  });
  const empty = state.sessionView.query.trim() ? "No matching sessions"
    : state.sessionView.all ? "No approved sessions" : "No registered sessions";
  get("sessions").replaceChildren(...(sessionButtons.length ? sessionButtons : [node("p", empty, "empty")]));
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
  renderWorkspaceSelection();
  lifecycle.workspace(state.workspace.retired_sessions ?? []);
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
  const from = new Map(Array.from(get("sessions").querySelectorAll("button"), (button) =>
    [button.dataset.session, button.getBoundingClientRect()]));
  const to = new Map(Array.from(get("rooms").querySelectorAll("button"), (button) =>
    [button.dataset.room, button.getBoundingClientRect()]));
  const origin = stage.getBoundingClientRect();
  const [sessions, rooms] = ["session-scroll", "room-scroll"].map((id) => {
    const panel = get(id);
    const top = panel.getBoundingClientRect().top + panel.clientTop;
    return {top, bottom: top + panel.clientHeight};
  });
  for (const room of state.workspace.rooms) {
    const end = to.get(room.id);
    if (!end) continue;
    const endY = end.top + end.height / 2;
    if (endY < rooms.top || endY >= rooms.bottom) continue;
    for (const membership of room.memberships) {
      if ((state.focused && membership.session !== state.focused) ||
          (state.selectedRoom && room.id !== state.selectedRoom)) continue;
      const start = from.get(membership.session);
      if (!start) continue;
      const startY = start.top + start.height / 2;
      if (startY < sessions.top || startY >= sessions.bottom) continue;
      const x1 = start.right - origin.left;
      const x2 = end.left - origin.left;
      const y1 = startY - origin.top;
      const y2 = endY - origin.top;
      const bend = Math.max(6, (x2 - x1) * .46);
      const path = svg("path");
      path.dataset.focused = String(Boolean(state.focused || state.selectedRoom));
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
  if (page !== "room") {
    tasks.reset();
    focused.reset();
  }
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
  const joined = readyMemberships(room).map(({session}) => sessionFor(session));
  const focusChooser = get("focus-actor");
  focusChooser.disabled = joined.length === 0;
  renderSessionChoices(focusChooser, joined, state.focusActor, "No available session");
  const membership = room.memberships.find((item) => item.session === state.reader);
  get("room-heading").textContent = room.name;
  get("room-heading").hidden = Boolean(membership);
  get("archive-room").hidden = !roomLeader(room);
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

function openRoom(id, preferred = state.focused) {
  if (state.room === id && state.page === "room") return;
  const room = state.workspace.rooms.find((item) => item.id === id);
  if (!room) return;
  tasks.reset();
  state.room = id;
  state.reader = null;
  state.actor = null;
  selectRoomSubjects(room, preferred);
  state.chat = null;
  members.reset();
  show("room");
  renderRoomHeader();
  renderChat();
  refreshRoomView();
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
  leadership.observe(snapshot);
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
    refreshRoomView();
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
  closeSessionMenu();
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

const adminEntry = get("admin-entry");

function showAdminEntry(url) {
  adminEntry.hidden = !url;
  if (url) adminEntry.href = url;
  else adminEntry.removeAttribute("href");
}

async function refreshAdminEntry(request) {
  let url;
  try {
    ({admin_url: url} = await read("/api/access"));
  } catch {}
  if (request === workspaceRequest) showAdminEntry(url);
}

adminEntry.addEventListener("click", async (event) => {
  if (!desktop) return;
  event.preventDefault();
  try {
    if ((await read("/api/open-admin", null, {})).state === "forbidden") showAdminEntry();
  } catch {}
});

async function refresh() {
  clearTimeout(workspaceTimer);
  const request = ++workspaceRequest;
  let expire = false;
  try {
    const snapshot = await read("/api/workspace");
    if (request === workspaceRequest) {
      displayWorkspace();
      applyWorkspace(snapshot);
      void refreshAdminEntry(request);
    }
  } catch (error) {
    if (request !== workspaceRequest) return;
    showAdminEntry();
    if (desktop) void refreshAdminEntry(request);
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
get("archive-room").addEventListener("click", markGoalReached);
get("open-room-archive").addEventListener("click", openRoomArchive);
archiveSession.addEventListener("change", () => loadArchiveRooms());
archiveRoom.addEventListener("change", () => loadArchivedRoom());
get("room-archive-more").addEventListener("click", () => loadArchiveRooms(archiveAfter));
get("room-archive-older").addEventListener("click", () => loadArchivedRoom(archiveBefore));
archiveDialog.addEventListener("close", () => {
  archiveListRequest += 1;
  archiveDetailRequest += 1;
});
get("back").addEventListener("click", () => show("workspace"));
function changeSessionView(update) {
  Object.assign(state.sessionView, update);
  state.focused = null;
  get("session-scroll").scrollTop = 0;
  renderWorkspace();
}

get("all-sessions").addEventListener("click", () =>
  changeSessionView({all: !state.sessionView.all, page: 0}));
get("session-search").addEventListener("input", (event) =>
  changeSessionView({query: event.target.value, page: 0}));
get("previous-sessions").addEventListener("click", () =>
  changeSessionView({page: state.sessionView.page - 1}));
get("next-sessions").addEventListener("click", () =>
  changeSessionView({page: state.sessionView.page + 1}));
get("sessions").addEventListener("contextmenu", openSessionMenu);
get("sessions").addEventListener("keydown", (event) => {
  if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) openSessionMenu(event);
});
get("sessions").addEventListener("click", (event) => {
  const clicked = event.target.closest("[data-session]");
  if (!clicked) return;
  state.focused = state.focused === clicked.dataset.session ? null : clicked.dataset.session;
  state.selectedRoom = null;
  renderWorkspace();
});
retireIdentity.addEventListener("click", () => {
  if (!sessionMenuTarget) return;
  const {session, trigger} = sessionMenuTarget;
  closeSessionMenu(true);
  lifecycle.confirmRetirement(session, trigger.dataset.label);
});
for (const type of ["pointerdown", "click", "focusin"]) {
  document.addEventListener(type, (event) => {
    if (!sessionMenu.contains(event.target)) closeSessionMenu();
  }, true);
}
document.addEventListener("scroll", closeMovedSessionMenu, {capture:true, passive:true});
window.addEventListener("blur", () => closeSessionMenu());
let firstRoomClick = null;
get("rooms").addEventListener("click", (event) => {
  if (event.detail <= 1) firstRoomClick = null;
  const clicked = event.target.closest("[data-room]");
  if (!clicked) return;
  const room = clicked.dataset.room;
  if (state.page === "room") {
    openRoom(room);
  } else if (state.page === "workspace" && event.detail <= 1) {
    if (event.detail === 1) firstRoomClick = {room, session: state.focused};
    state.focused = null;
    state.selectedRoom = state.selectedRoom === room ? null : room;
    // Keep the card in place so the second click can reach the same button.
    renderWorkspaceSelection();
    drawWires();
  }
});
get("rooms").addEventListener("dblclick", (event) => {
  const clicked = event.target.closest("[data-room]");
  const first = firstRoomClick;
  firstRoomClick = null;
  // A refresh can move a different room under the pointer between clicks.
  if (first && state.page === "workspace" && first.room === clicked?.dataset.room) {
    openRoom(first.room, first.session);
  }
});
get("rooms").addEventListener("keydown", (event) => {
  const clicked = event.target.closest("[data-room]");
  if (!clicked || event.key !== "Enter" || state.page !== "workspace") return;
  event.preventDefault();
  openRoom(clicked.dataset.room);
});
get("tasks-tab").addEventListener("click", () => setRoomView("tasks"));
get("focus-tab").addEventListener("click", () => setRoomView("focus"));
get("chats-tab").addEventListener("click", () => setRoomView("chats"));
get("task-actor").addEventListener("change", (event) => {
  const room = activeRoom();
  if (state.roomView !== "tasks" || !room || !readyMemberships(room).some((item) =>
    item.session === event.target.value && acknowledged(item))) return;
  state.actor = event.target.value;
  tasks.changeActor();
});
get("focus-actor").addEventListener("change", (event) => {
  const room = activeRoom();
  if (state.roomView !== "focus" || !room || !readyMemberships(room).some((item) =>
    item.session === event.target.value)) return;
  state.focusActor = event.target.value;
  focused.refresh();
});
get("copy-room").addEventListener("click", copyId);
get("older").addEventListener("click", () => {
  if (state.chat?.older) refreshRoom(state.chat.older);
});
const wireLayout = new ResizeObserver(() => {
  closeMovedSessionMenu();
  drawWires();
});
for (const area of ["session-scroll", "room-scroll"]) {
  const panel = get(area);
  panel.addEventListener("scroll", drawWires, {passive:true});
  wireLayout.observe(panel);
}
wireLayout.observe(get("sessions"));
get("retired-sessions").addEventListener("toggle", drawWires);
window.addEventListener("resize", () => {
  closeSessionMenu(true);
  drawWires();
});
window.addEventListener("keydown", (event) => {
  if (sessionMenuTarget && event.key === "Tab") closeSessionMenu(true);
  if (event.key !== "Escape") return;
  if (sessionMenuTarget) {
    event.preventDefault();
    closeSessionMenu(true);
  } else if (state.page !== "workspace" && !get("leadership-dialog").open) {
    show("workspace");
  }
});
if (desktop || key) {
  refresh();
} else {
  setConnection("Open the URL printed by TeamUp Client", false);
  renderWorkspace();
}
