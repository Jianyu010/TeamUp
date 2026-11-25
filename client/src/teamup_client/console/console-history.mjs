// Server IDs are decimal strings and can exceed Number.MAX_SAFE_INTEGER.
function compareIds(left, right) {
  return left.length - right.length || left.localeCompare(right);
}

export function sameBoard(left, right) {
  return left?.current === right?.current && left?.acknowledged === right?.acknowledged
    && left?.membership_id === right?.membership_id;
}

export async function readTranscript(previous, before, fetchPage) {
  let truncated = previous?.history_truncated === true;
  const latest = async () => {
    const page = await fetchPage({});
    if (page.state === "ready" && sameBoard(previous?.board, page.board)) {
      page.history_truncated ||= truncated;
    }
    return {page, reset: true};
  };
  const cached = previous?.messages;
  if (previous?.state !== "ready" || !cached.length) return latest();

  // Retention can leave holes below the expiry watermark. Above it, check one
  // cached page per poll to refresh sender labels without rereading the whole view.
  const newest = cached.at(-1).id;
  const all = new Map();
  const first = String(BigInt(cached[0].id) - 1n);
  const cycleEnd = previous.recheck?.through ?? newest;
  let after = before ? first : previous.recheck?.after ?? first;
  let probe = await fetchPage({after});
  const watermark = probe.expired_through_message_id;
  const rotate = !before && (!watermark || compareIds(cached[0].id, watermark) > 0);
  if (probe.state === "ready" && !rotate && after !== first) {
    after = first;
    probe = await fetchPage({after});
  }
  let recheck;
  while (true) {
    if (probe.state !== "ready") return {page: probe, reset: true};
    if (!sameBoard(previous.board, probe.board) || probe.expired_through_message_id !== watermark) return latest();
    truncated ||= probe.history_truncated === true;
    for (const item of probe.messages) all.set(item.id, item);
    const last = probe.messages.at(-1)?.id;
    if (probe.more_recent && (!last || compareIds(last, after) <= 0)) return latest();
    if (rotate) {
      const through = probe.more_recent ? last : newest;
      for (const item of cached) {
        if (compareIds(item.id, after) <= 0 || compareIds(item.id, through) > 0) {
          all.set(item.id, item);
        }
      }
      if (compareIds(through, cycleEnd) < 0) recheck = {after: through, through: cycleEnd};
      break;
    }
    if (!probe.more_recent || compareIds(last, newest) >= 0) break;
    after = last;
    probe = await fetchPage({after});
  }

  const page = await fetchPage(before ? {before} : {after: cached.at(-1).id});
  if (page.state !== "ready") return {page, reset: true};
  if (!sameBoard(probe.board, page.board) || page.expired_through_message_id !== watermark) return latest();

  for (const item of page.messages) all.set(item.id, item);
  page.messages = [...all.values()].sort((a, b) => compareIds(a.id, b.id));
  if (!before) page.older = previous.older;
  // Console page comparisons should only see data that affects the rendered chat.
  if (recheck) Object.defineProperty(page, "recheck", {value: recheck});
  page.history_truncated ||= truncated;
  return {page, reset: false};
}


export function messageRow(node, message, expanded = false) {
  const date = message.created_at ? new Date(message.created_at) : null;
  const knownTime = date && !Number.isNaN(date.getTime());
  const time = node("time", knownTime
    ? date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "—");
  if (knownTime) {
    time.dateTime = message.created_at;
    time.title = date.toLocaleString();
  }

  const recipients = message.mentioned_recipients ?? (message.mentioned ? [message.mentioned] : []);
  const labels = recipients.slice(0, 2).join(", ");
  const routing = node("div", undefined, "message-routing");
  routing.append(node("span", labels ? message.sender + " → " + labels : message.sender));
  if (recipients.length > 2) {
    const more = node("details", undefined, "mention-overflow");
    more.dataset.id = message.id;
    more.open = expanded;
    const summary = node("summary", "…");
    const list = node("span", recipients.join(", "), "mention-recipients");
    list.id = "message-recipients-" + message.id;
    summary.setAttribute("aria-label", "All recipients");
    summary.setAttribute("aria-describedby", list.id);
    more.append(summary, list);
    routing.append(more);
  }

  const heading = /^#[\t ]+([^\r\n]+)\r?\n[\t ]*\r?\n/.exec(message.text);
  const hasTitle = heading && heading[1].trim() && heading[0].length < message.text.length;
  const title = hasTitle ? heading[1].trim() : "#" + message.id;
  const body = hasTitle ? message.text.slice(heading[0].length) : message.text;
  const content = node("div", undefined, "message-content");
  content.append(routing, node("strong", title, "message-title"), node("p", body));
  const row = node("article", undefined, "message");
  row.append(time, content);
  return row;
}
