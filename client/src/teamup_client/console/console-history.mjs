// Server IDs are decimal strings and can exceed Number.MAX_SAFE_INTEGER.
function compareIds(left, right) {
  return left.length - right.length || left.localeCompare(right);
}

export function sameBoard(left, right) {
  return left?.current === right?.current && left?.acknowledged === right?.acknowledged;
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

  // A new membership can narrow history between polls with no change in board
  // version. A fresh prefix also preserves legacy rows that survive below an
  // expiry watermark, which records the greatest deleted ID, not every deletion.
  const newest = cached.at(-1).id;
  const all = new Map();
  let after = String(BigInt(cached[0].id) - 1n);
  let probe = await fetchPage({after});
  const watermark = probe.expired_through_message_id;
  while (true) {
    if (probe.state !== "ready") return {page: probe, reset: true};
    if (!sameBoard(previous.board, probe.board) || probe.expired_through_message_id !== watermark) return latest();
    truncated ||= probe.history_truncated === true;
    for (const item of probe.messages) all.set(item.id, item);
    if (!probe.more_recent) break;

    const last = probe.messages.at(-1)?.id;
    if (!last || compareIds(last, after) <= 0) return latest();
    if (compareIds(last, newest) >= 0) break;
    if (!watermark || compareIds(last, watermark) >= 0) {
      for (const item of cached) {
        if (compareIds(item.id, last) > 0) all.set(item.id, item);
      }
      break;
    }
    after = last;
    probe = await fetchPage({after});
  }

  const page = await fetchPage(before ? {before} : {after: cached.at(-1).id});
  if (page.state !== "ready") return {page, reset: true};
  if (!sameBoard(probe.board, page.board) || page.expired_through_message_id !== watermark) return latest();

  for (const item of page.messages) all.set(item.id, item);
  page.messages = [...all.values()].sort((a, b) => compareIds(a.id, b.id));
  if (!before) page.older = previous.older;
  page.history_truncated ||= truncated;
  return {page, reset: false};
}
