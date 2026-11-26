const pageSize = 8;
const registered = (session) => session.state === "ready" || Boolean(session.member_id);

export function sessionPage(workspace, {all = false, query = "", page = 0, focused = null} = {}) {
  const joined = new Set();
  const active = new Set();
  for (const room of workspace.rooms) {
    for (const membership of room.memberships) {
      joined.add(membership.session);
      if (membership.active_task === true) active.add(membership.session);
    }
  }
  const priority = (session) => active.has(session.id) ? 0 : joined.has(session.id) ? 1
    : registered(session) ? 2 : 3;
  const term = query.trim().toLocaleLowerCase();
  const matches = workspace.sessions.filter((session) => (all || registered(session)) &&
    (!term || [session.label, session.harness, session.id].some((text) =>
      text.toLocaleLowerCase().includes(term))));
  matches.sort((a, b) => priority(a) - priority(b) ||
    a.label.localeCompare(b.label, undefined, {numeric: true, sensitivity: "base"}) ||
    a.id.localeCompare(b.id));
  const pages = Math.max(1, Math.ceil(matches.length / pageSize));
  const selected = matches.findIndex((session) => session.id === focused);
  page = selected >= 0 ? Math.floor(selected / pageSize) : Math.max(0, Math.min(page, pages - 1));
  const offset = page * pageSize;
  return {
    sessions: matches.slice(offset, offset + pageSize),
    page,
    pages,
    count: matches.length,
    start: matches.length ? offset + 1 : 0,
    end: Math.min(offset + pageSize, matches.length),
    registered: workspace.sessions.filter(registered).length,
    total: workspace.sessions.length,
  };
}
