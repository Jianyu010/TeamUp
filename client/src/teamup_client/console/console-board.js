export function roomBoard({get, request}) {
  const toggle = get("board-toggle");
  const panel = get("board-panel");
  const chooser = get("board-session");
  const feedback = get("board-feedback");
  const form = get("board-form");
  const rules = get("board-rules-input");
  const protocol = get("board-protocol-input");
  const publish = get("board-publish");
  const edit = get("board-edit");
  const cancel = get("board-cancel");

  let room = null;
  let sessions = [];
  let viewer = null;
  let board = null;
  let editingVersion = null;
  let pending = null;

  function render() {
    const version = Math.max(viewer?.current ?? 0, board?.version ?? 0);
    const acknowledgment = viewer?.acknowledged === version ? "Acknowledged" : "Acknowledgment needed";
    const scope = viewer && sessions.length > 1 ? viewer.label + " · " : "";
    get("board-status").textContent = viewer ? "Version " + version + " · " + scope + acknowledgment : "";
    const current = board?.version === version ? board : null;
    get("board-rules").textContent = current?.rules ?? "";
    get("board-protocol").textContent = current?.protocol ?? "";
    get("board-view").hidden = !current || editingVersion !== null;
    edit.hidden = current?.can_edit !== true;
    form.hidden = editingVersion === null;
  }

  function setSubmitting(active) {
    cancel.disabled = active;
    rules.readOnly = active;
    protocol.readOnly = active;
  }

  function clear() {
    pending = null;
    board = null;
    editingVersion = null;
    edit.disabled = false;
    publish.disabled = false;
    setSubmitting(false);
    form.reset();
    feedback.textContent = "";
  }

  function expand(open) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  }

  function unavailable() {
    clear();
    feedback.textContent = "The room board is unavailable for this session.";
    render();
  }

  async function refresh() {
    if (!viewer || panel.hidden || pending || editingVersion !== null) return;
    const read = pending = Symbol();
    edit.disabled = true;
    if (!board || board.version < viewer.current) feedback.textContent = "Loading board…";
    try {
      const result = await request("/api/room-board", {room, session: viewer.id});
      if (read !== pending) return;
      if (result.state !== "ready") {
        unavailable();
        return;
      }
      if (result.board.version < Math.max(viewer.current, board?.version ?? 0)) return;
      if (!board || board.version !== result.board.version) feedback.textContent = "";
      board = result.board;
      render();
    } catch {
      if (read === pending) unavailable();
    } finally {
      if (read === pending) {
        pending = null;
        edit.disabled = false;
      }
    }
  }

  function select(next) {
    if (viewer?.id !== next?.id || viewer?.member_id !== next?.member_id ||
        viewer?.membership_id !== next?.membership_id) {
      clear();
    } else if (viewer && next.current > viewer.current && editingVersion === null) {
      pending = null;
    }
    viewer = next;
    render();
    refresh();
  }

  function setRoom(identifier, available, preferred) {
    if (room !== identifier) {
      room = identifier;
      viewer = null;
      clear();
      expand(false);
    }
    sessions = available;
    const next = sessions.find(({id}) => id === viewer?.id)
      ?? sessions.find(({id}) => id === preferred) ?? sessions[0] ?? null;
    chooser.replaceChildren(...sessions.map((session) => {
      const duplicate = sessions.some(({id, label}) => id !== session.id && label === session.label);
      return new Option(duplicate ? session.label + " · " + session.id : session.label,
        session.id, false, session.id === next?.id);
    }));
    get("board-session-label").hidden = sessions.length < 2;
    get("board-title").hidden = !next;
    if (!next) expand(false);
    select(next);
  }

  toggle.addEventListener("click", () => {
    expand(panel.hidden);
    refresh();
  });
  chooser.addEventListener("change", () => {
    select(sessions.find(({id}) => id === chooser.value) ?? null);
  });
  edit.addEventListener("click", () => {
    if (board?.can_edit !== true || pending) return;
    editingVersion = board.version;
    get("board-edit-version").textContent = "Editing version " + editingVersion;
    rules.value = board.rules;
    protocol.value = board.protocol;
    feedback.textContent = "";
    render();
    rules.focus();
  });
  cancel.addEventListener("click", () => {
    clear();
    render();
    refresh();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!viewer || editingVersion === null || publish.disabled || pending) return;
    const write = pending = Symbol();
    publish.disabled = true;
    setSubmitting(true);
    feedback.textContent = "Publishing…";
    const body = {version: editingVersion, rules: rules.value, protocol: protocol.value};
    try {
      const result = await request("/api/room-board", {room, session: viewer.id}, {body});
      if (write !== pending) return;
      if (result.state === "ready") {
        clear();
        board = result.board;
        feedback.textContent = "Board published. Members need to acknowledge the new version.";
        render();
        edit.focus();
      } else if (result.state === "conflict") {
        feedback.textContent = "A newer version was published. Close the editor to load it.";
      } else if (["not_found", "not_registered", "unauthorized", "forbidden"].includes(result.state)) {
        unavailable();
      } else {
        publish.disabled = false;
        feedback.textContent = result.state === "invalid_request"
          ? "Rules and Protocol must each contain text and be at most 8,000 characters."
          : "Publishing is temporarily unavailable.";
      }
    } catch {
      if (write === pending) {
        publish.disabled = false;
        feedback.textContent = "Publishing is temporarily unavailable.";
      }
    } finally {
      if (write === pending) {
        pending = null;
        setSubmitting(false);
      }
    }
  });

  return {setRoom, refresh};
}
