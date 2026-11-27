"use strict";

const LOGIN_PATH = "/v1/auth/login";
const desktopSignIn = globalThis.teamupAdmin?.signIn;
const CONSOLE_BASE = "/v1/console";
const PAGE_LIMIT = 50;
const CI_DASHBOARDS_URL = "https://jianyu010.github.io/TeamUp/";
const ENTRY_VIEW = location.pathname === "/dashboard/" ? "maintenance" : "rooms";

const KIND_LABELS = {
  message: "message",
  member_joined: "member joined",
  board_acknowledged: "board acknowledged",
  delivery: "delivery",
};

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

const state = {
  epoch: 0,
  session: null,
  expiryTimer: null,
  view: null,
  rooms: [],
  roomsPane: { loading: false, denied: false, error: "" },
  room: null,
  tab: "timeline",
  controllers: [],
};

const els = {
  loginView: document.getElementById("loginView"),
  loginForm: document.getElementById("loginForm"),
  loginCredentials: document.getElementById("loginCredentials"),
  username: document.getElementById("username"),
  password: document.getElementById("password"),
  loginStatus: document.getElementById("loginStatus"),
  loginSubmit: document.getElementById("loginSubmit"),
  mainView: document.getElementById("mainView"),
  maintenanceView: document.getElementById("maintenanceView"),
  consoleViews: document.getElementById("consoleViews"),
  roomsViewButton: document.getElementById("roomsViewButton"),
  maintenanceViewButton: document.getElementById("maintenanceViewButton"),
  accountsView: document.getElementById("accountsView"),
  accountsViewButton: document.getElementById("accountsViewButton"),
  ciView: document.getElementById("ciView"),
  ciViewButton: document.getElementById("ciViewButton"),
  pageTitle: document.getElementById("pageTitle"),
  statusLine: document.getElementById("statusLine"),
  sessionActions: document.getElementById("sessionActions"),
  refreshButton: document.getElementById("refreshButton"),
  logoutButton: document.getElementById("logoutButton"),
  roomList: document.getElementById("roomList"),
  roomEmpty: document.getElementById("roomEmpty"),
  roomDetail: document.getElementById("roomDetail"),
  roomTitle: document.getElementById("roomTitle"),
  roomMeta: document.getElementById("roomMeta"),
  roomId: document.getElementById("roomId"),
  rosterBody: document.getElementById("rosterBody"),
  tabBar: document.getElementById("tabBar"),
  tabTimeline: document.getElementById("tabTimeline"),
  tabChats: document.getElementById("tabChats"),
  panelTimeline: document.getElementById("panelTimeline"),
  panelChats: document.getElementById("panelChats"),
  timelineList: document.getElementById("timelineList"),
  timelineStatus: document.getElementById("timelineStatus"),
  timelineMore: document.getElementById("timelineMore"),
  chatsDenied: document.getElementById("chatsDenied"),
  chatsStatus: document.getElementById("chatsStatus"),
  chatsList: document.getElementById("chatsList"),
  chatsMore: document.getElementById("chatsMore"),
};

const maintenance = createMaintenanceView({
  api, element, failureText, sessionExpired, basePath: CONSOLE_BASE,
});

const accounts = createAccountsView({
  api, element, sessionExpired, basePath: CONSOLE_BASE,
  accessDenied: () => {
    els.accountsViewButton.hidden = true;
    selectView("rooms");
    setTopStatus("Account access changed.");
  },
});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setTopStatus(text) {
  els.statusLine.textContent = text;
}

function setLoginStatus(text) {
  els.loginStatus.textContent = text;
}

function newEpoch() {
  state.epoch += 1;
  abortPending();
  return state.epoch;
}

function abortPending() {
  for (const controller of state.controllers) {
    controller.abort();
  }
  state.controllers = [];
}

async function api(path, options) {
  const settings = options || {};
  if (expireIfDue()) {
    return { status: 401, payload: null };
  }
  const session = state.session;
  const controller = settings.controller || new AbortController();
  state.controllers.push(controller);
  try {
    const headers = {};
    if (session) {
      headers.Authorization = "Bearer " + session.token;
    }
    if (settings.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    let response = null;
    try {
      response = await fetch(path, {
        method: settings.method || "GET",
        headers,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        signal: controller.signal,
        body: settings.body === undefined ? undefined : JSON.stringify(settings.body),
      });
    } catch {
      return { status: 0, payload: null };
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!controller.signal.aborted && session === state.session && expireIfDue()) {
      return { status: 401, payload: null };
    }
    return { status: response.status, payload };
  } finally {
    const at = state.controllers.indexOf(controller);
    if (at !== -1) {
      state.controllers.splice(at, 1);
    }
  }
}

function errorCode(result) {
  if (
    result &&
    result.payload &&
    result.payload.error &&
    typeof result.payload.error.code === "string"
  ) {
    return result.payload.error.code;
  }
  return "";
}

function failureText(result, action) {
  if (result.status === 0) {
    return "Cannot reach the server. The console API may not be deployed at this origin yet.";
  }
  const code = errorCode(result);
  return code ? action + " failed (" + code + ")." : action + " failed.";
}

function sameDay(left, right) {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}

function formatTime(at) {
  const seconds = Number(at);
  if (at === null || at === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const clock =
    String(date.getHours()).padStart(2, "0") +
    ":" +
    String(date.getMinutes()).padStart(2, "0");
  if (sameDay(new Date(), date)) {
    return clock;
  }
  return MONTHS[date.getMonth()] + " " + date.getDate() + ", " + clock;
}

function shortId(id) {
  return typeof id === "string" ? id.slice(0, 8) : String(id);
}

function expireIfDue() {
  const deadline = state.session?.expiresAt;
  if (!deadline || Date.now() < deadline * 1000) return false;
  sessionExpired();
  return true;
}

function scheduleSessionExpiry() {
  const deadline = state.session?.expiresAt;
  if (!deadline || expireIfDue()) return;
  const delay = Math.min(Math.max(1, deadline * 1000 - Date.now()), 2147483647);
  state.expiryTimer = setTimeout(scheduleSessionExpiry, delay);
}

function sessionExpired() {
  if (state.session === null) {
    return;
  }
  newEpoch();
  showLogin("Your session has ended. Sign in again.");
}

function logout() {
  newEpoch();
  showLogin("Signed out. The in-memory token was cleared.");
}

function clearRoomPanes() {
  els.roomList.replaceChildren();
  els.roomEmpty.hidden = false;
  els.roomDetail.hidden = true;
  els.roomTitle.textContent = "";
  els.roomMeta.textContent = "";
  els.roomId.textContent = "";
  els.rosterBody.replaceChildren();
  els.timelineStatus.className = "pane-status";
  els.timelineStatus.textContent = "";
  els.timelineList.replaceChildren();
  els.timelineMore.hidden = true;
  els.chatsDenied.hidden = true;
  els.chatsDenied.textContent = "";
  els.chatsStatus.className = "pane-status";
  els.chatsStatus.textContent = "";
  els.chatsList.replaceChildren();
  els.chatsMore.hidden = true;
}

function showLogin(message) {
  clearTimeout(state.expiryTimer);
  state.expiryTimer = null;
  state.session = null;
  state.view = null;
  state.room = null;
  state.rooms = [];
  state.roomsPane = { loading: false, denied: false, error: "" };
  clearRoomPanes();
  maintenance.close();
  accounts.close();
  els.accountsView.hidden = true;
  els.accountsViewButton.hidden = true;
  els.maintenanceViewButton.hidden = true;
  els.ciView.replaceChildren();
  els.ciView.hidden = true;
  els.pageTitle.textContent = "TeamUp";
  els.mainView.hidden = true;
  els.maintenanceView.hidden = true;
  els.consoleViews.hidden = true;
  els.loginView.hidden = false;
  els.sessionActions.hidden = true;
  els.loginSubmit.disabled = false;
  els.password.value = "";
  setLoginStatus(message);
  setTopStatus("Signed out");
  (desktopSignIn ? els.loginSubmit : els.username).focus();
}

function showConsole(capabilities) {
  els.pageTitle.textContent = "Admin";
  els.maintenanceViewButton.hidden = !capabilities.maintenance;
  els.accountsViewButton.hidden = !capabilities.accounts;
  els.loginView.hidden = true;
  els.consoleViews.hidden = false;
  els.sessionActions.hidden = false;
  els.roomEmpty.hidden = false;
  els.roomDetail.hidden = true;
  const ends = state.session.expiresAt
    ? formatTime(state.session.expiresAt)
    : "";
  setTopStatus(
    "Signed in as " +
      state.session.username +
      (ends ? " · session ends " + ends : "")
  );
  const initial = capabilities.maintenance ? ENTRY_VIEW : "rooms";
  selectView(initial);
  if (initial === "maintenance") {
    els.maintenanceViewButton.focus();
  } else {
    els.roomsViewButton.focus();
  }
}

function selectView(view) {
  if (!state.session || state.view === view) return;
  const epoch = newEpoch();
  state.view = view;
  state.room = null;
  state.rooms = [];
  clearRoomPanes();
  const showMaintenance = view === "maintenance";
  els.mainView.hidden = view !== "rooms";
  els.maintenanceView.hidden = !showMaintenance;
  els.accountsView.hidden = view !== "accounts";
  els.ciView.hidden = view !== "ci";
  els.roomsViewButton.setAttribute("aria-current", String(view === "rooms"));
  els.maintenanceViewButton.setAttribute("aria-current", String(showMaintenance));
  els.accountsViewButton.setAttribute("aria-current", String(view === "accounts"));
  els.ciViewButton.setAttribute("aria-current", String(view === "ci"));
  if (showMaintenance) maintenance.open();
  else maintenance.close();
  if (view === "accounts") void accounts.refresh();
  else accounts.close();
  if (view === "ci") loadCiDashboard();
  else els.ciView.replaceChildren();
  if (view === "rooms" || showMaintenance) loadRoomsList(epoch);
}

function loadCiDashboard() {
  const frame = element("iframe", "ci-dashboard");
  frame.title = "TeamUp CI Dashboards";
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.src = CI_DASHBOARDS_URL;
  els.ciView.replaceChildren(frame);
}

async function requestDesktopSignIn() {
  try {
    return await desktopSignIn();
  } catch {
    return {status: 0, payload: null};
  }
}

async function submitLogin(event) {
  event?.preventDefault();
  if (state.session || els.loginSubmit.disabled) return;
  const username = els.username.value.trim();
  const password = els.password.value;
  if (!desktopSignIn && (!username || !password)) {
    setLoginStatus("Enter both a username and a password.");
    return;
  }
  setLoginStatus("Signing in…");
  els.loginSubmit.disabled = true;
  const epoch = newEpoch();
  const result = desktopSignIn ? await requestDesktopSignIn() : await api(LOGIN_PATH, {
    method: "POST",
    body: { username, password, "purpose": "console" },
  });
  if (epoch !== state.epoch) {
    return;
  }
  els.loginSubmit.disabled = false;
  const payload = result.payload;
  const token =
    payload && typeof payload["access_token"] === "string"
      ? payload["access_token"]
      : "";
  if (result.status === 200 && token) {
    state.session = {
      token,
      username: desktopSignIn ? payload.username : username,
      expiresAt: Number(payload["expires_at"]) || 0,
    };
    els.password.value = "";
    if (expireIfDue()) return;
    els.loginSubmit.disabled = true;
    const capabilities = await api(CONSOLE_BASE + "/capabilities");
    if (epoch !== state.epoch) return;
    if (capabilities.status !== 200 || !capabilities.payload) {
      showLogin("Sign-in failed. Check your account and try again.");
      return;
    }
    els.loginSubmit.disabled = false;
    scheduleSessionExpiry();
    showConsole(capabilities.payload);
    return;
  }
  if (desktopSignIn && [401, 403, 409].includes(result.status)) {
    setLoginStatus("Sign in to the Client Console with an operator account, then try again.");
  } else if (desktopSignIn && result.status === 404) {
    setLoginStatus("Update the TeamUp Server to use automatic Admin sign-in.");
  } else if (result.status === 401) {
    setLoginStatus("Sign-in failed. Check your username and password.");
  } else if (result.status === 429) {
    setLoginStatus("Too many attempts. Wait a moment and try again.");
  } else if (result.status === 422) {
    setLoginStatus("The server rejected the sign-in request shape.");
  } else {
    setLoginStatus(failureText(result, "Sign-in"));
  }
}

async function loadRoomsList(epoch) {
  state.roomsPane = { loading: true, denied: false, error: "" };
  renderRooms();
  const result = await api(CONSOLE_BASE + "/rooms");
  if (epoch !== state.epoch || state.session === null) {
    return;
  }
  if (result.status === 401) {
    sessionExpired();
    return;
  }
  if (result.status === 403) {
    state.roomsPane = { loading: false, denied: true, error: "" };
    renderRooms();
    return;
  }
  if (result.status !== 200 || !result.payload || !Array.isArray(result.payload.rooms)) {
    state.roomsPane = {
      loading: false,
      denied: false,
      error: failureText(result, "Loading rooms"),
    };
    renderRooms();
    return;
  }
  state.roomsPane = { loading: false, denied: false, error: "" };
  state.rooms = result.payload.rooms;
  renderRooms();
}

function renderRooms() {
  if (state.view === "maintenance") {
    maintenance.setRooms(state.rooms, state.roomsPane);
    return;
  }
  const list = els.roomList;
  list.replaceChildren();
  if (state.roomsPane.loading) {
    list.append(element("p", "pane-note", "Loading rooms…"));
    return;
  }
  if (state.roomsPane.denied) {
    list.append(
      element("p", "pane-note error", "Console access is not granted to this account.")
    );
    return;
  }
  if (state.roomsPane.error) {
    const note = element("p", "pane-note error", state.roomsPane.error);
    const retryLine = element("p", "retry-line");
    const retry = element("button", "linkish", "Retry");
    retry.type = "button";
    retry.addEventListener("click", () => loadRoomsList(state.epoch));
    retryLine.append(retry);
    list.append(note, retryLine);
    return;
  }
  if (state.rooms.length === 0) {
    list.append(element("p", "pane-note", "No rooms yet."));
    return;
  }
  for (const room of state.rooms) {
    const selected = state.room !== null && state.room.id === room.room_id;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "room-card" + (selected ? " selected" : "");
    if (selected) {
      card.setAttribute("aria-current", "true");
    }
    card.append(element("span", "room-name", room.name));
    card.append(element("span", "room-meta", roomSummary(room)));
    card.addEventListener("click", () => selectRoom(room.room_id));
    list.append(card);
  }
}

function roomSummary(room) {
  const members =
    room.member_count + (room.member_count === 1 ? " member" : " members");
  if (room.archived_at) return members + " · archived";
  if (!room.last_activity) {
    return members + " · no activity";
  }
  const seen = formatTime(room.last_activity.at);
  return seen ? members + " · seen " + seen : members + " · seen unknown";
}

function freshRoom(roomId) {
  return {
    id: roomId,
    detail: null,
    members: [],
    transcriptAllowed: false,
    timeline: {
      rows: [],
      cursor: 0,
      done: false,
      loading: false,
      error: "",
      denied: false,
      loaded: false,
    },
    chats: {
      rows: [],
      hasMore: false,
      loading: false,
      error: "",
      denied: false,
      deniedText: "",
      loaded: false,
      retryBefore: undefined,
    },
  };
}

function selectRoom(roomId) {
  const epoch = newEpoch();
  state.room = freshRoom(roomId);
  renderDetailShell();
  renderRooms();
  setTab("timeline");
  loadRoomDetail(epoch);
}

function refreshAll() {
  if (state.session === null) {
    return;
  }
  if (state.view === "accounts") {
    void accounts.refresh();
    return;
  }
  if (state.view === "ci") {
    loadCiDashboard();
    return;
  }
  if (state.view === "maintenance") {
    maintenance.refresh();
    if (!state.roomsPane.loading && state.session) loadRoomsList(state.epoch);
    return;
  }
  const epoch = newEpoch();
  loadRoomsList(epoch);
  if (state.room !== null) {
    const roomId = state.room.id;
    const keepTab = state.tab;
    state.room = freshRoom(roomId);
    renderDetailShell();
    setTab(keepTab);
    loadRoomDetail(epoch);
  }
}

function renderDetailShell() {
  clearRoomPanes();
  els.roomEmpty.hidden = true;
  els.roomDetail.hidden = false;
  els.roomTitle.textContent = "Loading room…";
  els.chatsStatus.textContent = "Waiting for room details…";
  renderTimeline();
}

function renderDetailError(message) {
  clearRoomPanes();
  els.roomEmpty.hidden = true;
  els.roomDetail.hidden = false;
  els.roomTitle.textContent = "Room unavailable";
  els.timelineStatus.className = "pane-status error";
  els.timelineStatus.textContent = message;
  els.chatsStatus.textContent = "Room details unavailable.";
}

async function loadRoomDetail(epoch) {
  const room = state.room;
  if (!room) {
    return;
  }
  const result = await api(
    CONSOLE_BASE + "/rooms/" + encodeURIComponent(room.id)
  );
  if (epoch !== state.epoch || state.room !== room) {
    return;
  }
  if (result.status === 401) {
    sessionExpired();
    return;
  }
  if (result.status === 403) {
    renderDetailError("Console access is not granted for this room.");
    return;
  }
  if (result.status === 404) {
    renderDetailError("This room no longer exists. Use Refresh to reload the room list.");
    return;
  }
  const payload = result.payload;
  if (
    result.status !== 200 ||
    !payload ||
    !payload.room ||
    !Array.isArray(payload.members)
  ) {
    renderDetailError(failureText(result, "Loading the room"));
    return;
  }
  room.detail = payload.room;
  room.members = payload.members;
  room.transcriptAllowed = payload.transcript_allowed === true;
  renderRoomDetail();
}

function renderRoomDetail() {
  const room = state.room;
  const detail = room.detail;
  const count = detail.member_count;
  els.roomTitle.textContent = detail.name;
  els.roomMeta.textContent =
    String(count) + (count === 1 ? " member" : " members") + (detail.archived_at ? " · Archived room" : " · TeamUp room");
  els.roomId.textContent = "Room " + detail.room_id;
  els.rosterBody.replaceChildren();
  for (const entry of room.members) {
    const row = document.createElement("tr");
    let ackClass = "ack";
    let ackText = "Unknown";
    if (entry.board_state === "acknowledged") {
      ackClass = "ack acknowledged";
      ackText = "Acknowledged";
    } else if (entry.board_state === "awaiting") {
      ackClass = "ack awaiting";
      ackText = "Awaiting";
    }
    row.append(
      element("td", "member-role", entry.role_description),
      element("td", ackClass, ackText),
      element("td", "member-activity", memberActivityText(entry)),
      element("td", "member-harness", "Unknown")
    );
    els.rosterBody.append(row);
  }
  syncChatsGate();
  renderTimeline();
}

function memberActivityText(entry) {
  if (!entry.last_activity) {
    return "No activity";
  }
  const time = formatTime(entry.last_activity.at) || "unknown";
  const kind = KIND_LABELS[entry.last_activity.kind];
  return kind ? time + " · " + kind : time;
}

function syncChatsGate() {
  const room = state.room;
  if (!room || room.detail === null) {
    return;
  }
  const pane = room.chats;
  if (!room.transcriptAllowed && !pane.denied) {
    pane.denied = true;
    pane.deniedText =
      "Transcript access is not granted for this room. The console makes no transcript requests.";
    pane.rows = [];
    pane.loaded = false;
    renderChats();
    return;
  }
  ensureChatsLoaded();
}

function ensureChatsLoaded() {
  const room = state.room;
  if (!room || state.tab !== "chats" || room.detail === null) {
    return;
  }
  if (!room.transcriptAllowed) {
    return;
  }
  const pane = room.chats;
  if (pane.loaded || pane.loading || pane.error || pane.denied) {
    return;
  }
  loadChats(state.epoch);
}

function ensureTimelineLoaded() {
  const room = state.room;
  if (!room || state.tab !== "timeline") {
    return;
  }
  const pane = room.timeline;
  if (pane.loaded || pane.loading || pane.error || pane.denied) {
    return;
  }
  loadTimeline(state.epoch);
}

function setTab(tab) {
  state.tab = tab;
  const timeline = tab === "timeline";
  els.tabTimeline.classList.toggle("active", timeline);
  els.tabChats.classList.toggle("active", !timeline);
  els.tabTimeline.setAttribute("aria-selected", String(timeline));
  els.tabChats.setAttribute("aria-selected", String(!timeline));
  els.panelTimeline.hidden = !timeline;
  els.panelChats.hidden = timeline;
  if (timeline) {
    ensureTimelineLoaded();
  } else {
    ensureChatsLoaded();
  }
}

async function loadTimeline(epoch) {
  const room = state.room;
  if (!room) {
    return;
  }
  const pane = room.timeline;
  pane.loading = true;
  pane.error = "";
  renderTimeline();
  const query = new URLSearchParams();
  query.set("after", String(pane.cursor));
  query.set("limit", String(PAGE_LIMIT));
  const result = await api(
    CONSOLE_BASE +
      "/rooms/" +
      encodeURIComponent(room.id) +
      "/timeline?" +
      query.toString()
  );
  if (epoch !== state.epoch || state.room !== room) {
    return;
  }
  pane.loading = false;
  if (result.status === 401) {
    sessionExpired();
    return;
  }
  if (result.status === 403) {
    pane.denied = true;
    pane.rows = [];
    renderTimeline();
    return;
  }
  if (result.status !== 200 || !result.payload || !Array.isArray(result.payload.activities)) {
    pane.error = failureText(result, "Loading the timeline");
    renderTimeline();
    return;
  }
  const rows = result.payload.activities;
  pane.rows = pane.rows.concat(rows);
  if (rows.length > 0) {
    const last = rows[rows.length - 1];
    if (typeof last.activity_id === "number") {
      pane.cursor = last.activity_id;
    }
  }
  if (rows.length < PAGE_LIMIT) {
    pane.done = true;
  }
  pane.loaded = true;
  renderTimeline();
}

function renderTimeline() {
  const room = state.room;
  if (!room) {
    return;
  }
  const pane = room.timeline;
  const list = els.timelineList;
  list.replaceChildren();
  if (pane.denied) {
    els.timelineStatus.className = "pane-status";
    els.timelineStatus.textContent = "Timeline access is not granted for this room.";
    els.timelineMore.hidden = true;
    return;
  }
  if (pane.error) {
    els.timelineStatus.className = "pane-status";
    els.timelineStatus.textContent = "";
    for (const activity of pane.rows) {
      list.append(timelineRow(room, activity));
    }
    list.append(errorRow(pane.error, () => loadTimeline(state.epoch)));
    els.timelineMore.hidden = true;
    return;
  }
  if (pane.rows.length === 0) {
    els.timelineStatus.className = "pane-status";
    els.timelineStatus.textContent = pane.loaded
      ? "No activity recorded yet."
      : "Loading activity…";
    els.timelineMore.hidden = true;
    return;
  }
  els.timelineStatus.className = "pane-status";
  els.timelineStatus.textContent = pane.loading ? "Loading more activity…" : "";
  for (const activity of pane.rows) {
    list.append(timelineRow(room, activity));
  }
  els.timelineMore.hidden = pane.loading || pane.done;
}

function timelineRow(room, activity) {
  const li = document.createElement("li");
  li.append(element("span", "row-time", formatTime(activity.at) || "unknown"));
  li.append(element("span", "row-agent", KIND_LABELS[activity.kind] || "activity"));
  const text = element("span", "row-text", activityDetail(activity));
  if (
    activity.kind === "message" &&
    activity.message_id &&
    room.transcriptAllowed
  ) {
    const jump = element("button", "linkish", "Open in Chats");
    jump.type = "button";
    jump.addEventListener("click", () => setTab("chats"));
    text.append(jump);
  }
  li.append(text);
  return li;
}

function activityDetail(activity) {
  const parts = [];
  if (activity.actor_member_id) {
    parts.push("actor " + shortId(activity.actor_member_id));
  }
  if (activity.message_id) {
    parts.push("message msg-" + String(activity.message_id));
  }
  if (parts.length === 0) {
    parts.push("no details");
  }
  return parts.join(" · ");
}

async function loadChats(epoch, before) {
  const room = state.room;
  if (!room) {
    return;
  }
  const pane = room.chats;
  pane.loading = true;
  pane.error = "";
  renderChats();
  const query = new URLSearchParams();
  query.set("limit", String(PAGE_LIMIT));
  if (before !== undefined) {
    query.set("before", String(before));
  }
  const result = await api(
    CONSOLE_BASE +
      "/rooms/" +
      encodeURIComponent(room.id) +
      "/transcript?" +
      query.toString()
  );
  if (epoch !== state.epoch || state.room !== room) {
    return;
  }
  pane.loading = false;
  if (result.status === 401) {
    sessionExpired();
    return;
  }
  if (result.status === 403) {
    pane.denied = true;
    pane.deniedText =
      "Transcript access is not granted for this room. The console makes no transcript requests.";
    pane.rows = [];
    pane.loaded = false;
    pane.retryBefore = undefined;
    renderChats();
    return;
  }
  const payload = result.payload;
  if (result.status !== 200 || !payload || !Array.isArray(payload.messages)) {
    pane.error = failureText(result, "Loading messages");
    pane.retryBefore = before;
    renderChats();
    return;
  }
  if (before === undefined) {
    pane.rows = payload.messages;
  } else {
    pane.rows = payload.messages.concat(pane.rows);
  }
  pane.hasMore = payload.has_more === true;
  pane.retryBefore = undefined;
  pane.loaded = true;
  renderChats();
}

function renderChats() {
  const room = state.room;
  if (!room) {
    return;
  }
  const pane = room.chats;
  els.chatsList.replaceChildren();
  els.chatsDenied.hidden = !pane.denied;
  if (pane.denied) {
    els.chatsDenied.textContent = pane.deniedText;
    els.chatsStatus.className = "pane-status";
    els.chatsStatus.textContent = "";
    els.chatsMore.hidden = true;
    return;
  }
  if (pane.error) {
    els.chatsStatus.className = "pane-status";
    els.chatsStatus.textContent = "";
    for (const message of pane.rows) {
      els.chatsList.append(messageRow(message));
    }
    els.chatsList.append(errorRow(pane.error, retryChats));
    els.chatsMore.hidden = true;
    return;
  }
  if (pane.rows.length === 0) {
    els.chatsStatus.className = "pane-status";
    els.chatsStatus.textContent = pane.loaded
      ? "No messages yet."
      : pane.loading
        ? "Loading messages…"
        : "Waiting for room details…";
    els.chatsMore.hidden = true;
    return;
  }
  els.chatsStatus.className = "pane-status";
  els.chatsStatus.textContent = pane.loading ? "Loading older messages…" : "";
  for (const message of pane.rows) {
    els.chatsList.append(messageRow(message));
  }
  els.chatsMore.hidden = pane.loading || !pane.hasMore;
}

function retryChats() {
  const room = state.room;
  if (!room) {
    return;
  }
  loadChats(state.epoch, room.chats.retryBefore);
}

function messageRow(message) {
  const li = document.createElement("li");
  li.append(element("span", "row-time", formatTime(message.created_at) || "unknown"));
  const sender =
    typeof message.sender_role_description === "string"
      ? message.sender_role_description
      : "unknown member";
  const label = message.sender_retired === true ? `${sender} (retired)` : sender;
  li.append(element("span", "row-agent", label));
  li.append(element("span", "row-text", message.text));
  return li;
}

function errorRow(message, retryAction) {
  const li = document.createElement("li");
  li.className = "pane-error";
  li.append(element("span", "", message));
  const retry = element("button", "linkish", "Retry");
  retry.type = "button";
  retry.addEventListener("click", retryAction);
  li.append(retry);
  return li;
}

els.loginForm.addEventListener("submit", submitLogin);
els.logoutButton.addEventListener("click", logout);
els.refreshButton.addEventListener("click", refreshAll);
els.roomsViewButton.addEventListener("click", () => selectView("rooms"));
els.maintenanceViewButton.addEventListener("click", () => selectView("maintenance"));
els.accountsViewButton.addEventListener("click", () => selectView("accounts"));
els.ciViewButton.addEventListener("click", () => selectView("ci"));
document.addEventListener("visibilitychange", () => {
  if (!expireIfDue()) maintenance.visibilityChanged();
});
els.tabTimeline.addEventListener("click", () => setTab("timeline"));
els.tabChats.addEventListener("click", () => setTab("chats"));
els.tabBar.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
    return;
  }
  event.preventDefault();
  const next = state.tab === "timeline" ? "chats" : "timeline";
  setTab(next);
  if (next === "timeline") {
    els.tabTimeline.focus();
  } else {
    els.tabChats.focus();
  }
});
els.timelineMore.addEventListener("click", () => loadTimeline(state.epoch));
els.chatsMore.addEventListener("click", () => {
  const room = state.room;
  if (!room || room.chats.rows.length === 0) {
    return;
  }
  const oldest = room.chats.rows[0].message_id;
  if (typeof oldest === "number") {
    loadChats(state.epoch, oldest);
  }
});

setTab("timeline");

if (desktopSignIn) {
  els.loginCredentials.hidden = true;
  void submitLogin();
}
