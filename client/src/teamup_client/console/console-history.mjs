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
  let after = before ? first : previous.recheck_after ?? first;
  let probe = await fetchPage({after});
  const watermark = probe.expired_through_message_id;
  const rotate = !before && (!watermark || compareIds(cached[0].id, watermark) > 0);
  if (probe.state === "ready" && !rotate && after !== first) {
    after = first;
    probe = await fetchPage({after});
  }
  let recheckAfter;
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
      if (compareIds(through, newest) < 0) recheckAfter = through;
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
  if (recheckAfter) page.recheck_after = recheckAfter;
  page.history_truncated ||= truncated;
  return {page, reset: false};
}
