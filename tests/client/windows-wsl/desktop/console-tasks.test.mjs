import assert from "node:assert/strict";
import test from "node:test";
import {openTaskLanes} from "../../../../client/shared/src/teamup_client/console/console-tasks.js";

function task(task_id, participants, addressed = []) {
  return {task_id, participant_member_ids: participants, addressed_member_ids: addressed};
}

test("Ongoing includes every visible open task and Taken follows the acting member", () => {
  const tasks = [
    task("unassigned", []),
    task("assigned-local", [], ["second"]),
    task("assigned-remote", [], ["remote"]),
    task("first-session", ["first"]),
    task("second-session", ["second"]),
    task("remote-session", ["remote"]),
    task("shared", ["first", "second", "remote"]),
  ];
  const localMembers = ["first", "second"];

  for (const [actor, taken] of [
    ["first", ["first-session", "shared"]],
    ["second", ["second-session", "shared"]],
    [null, []],
  ]) {
    const lanes = Object.fromEntries(openTaskLanes(tasks, localMembers, actor)
      .map(([name, items]) => [name, items.map(({task_id}) => task_id)]));
    assert.deepEqual(lanes.Available, ["unassigned", "assigned-local"]);
    assert.deepEqual(lanes.Taken, taken);
    assert.deepEqual(lanes.Ongoing, tasks.map(({task_id}) => task_id));
  }
});
