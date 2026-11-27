import assert from "node:assert/strict";
import test from "node:test";
import {openTaskLanes} from "../../../../../../client/shared/src/teamup_client/console/console-tasks.js";

const first = {id: "first-session", member_id: "first"};
const second = {id: "second-session", member_id: "second"};

function task(task_id, participants, addressed = [], visible_to = [first.id, second.id]) {
  return {task_id, participant_member_ids: participants, addressed_member_ids: addressed, visible_to};
}

function laneIds(tasks, actor) {
  return Object.fromEntries(openTaskLanes(tasks, actor)
    .map(([name, items]) => [name, items.map(({task_id}) => task_id)]));
}

test("Available includes unjoined tasks visible only through their creator", () => {
  const tasks = [
    task("unassigned", []),
    task("assigned-local", [], ["second"]),
    task("assigned-remote", [], ["remote"], [first.id]),
  ];

  assert.deepEqual(laneIds(tasks, first), {
    Available: ["unassigned", "assigned-local", "assigned-remote"],
    Taken: [],
    Ongoing: [],
  });
});

test("first join moves a task from Available to Ongoing and Taken", () => {
  const open = task("unassigned", []);
  assert.deepEqual(laneIds([open], first), {
    Available: ["unassigned"],
    Taken: [],
    Ongoing: [],
  });

  const joined = {...open, participant_member_ids: ["first"]};
  assert.deepEqual(laneIds([joined], first), {
    Available: [],
    Taken: ["unassigned"],
    Ongoing: ["unassigned"],
  });
});

test("Taken follows the authorized acting session while Ongoing includes remote participants", () => {
  const tasks = [
    task("first-task", ["first"]),
    task("second-task", ["second"]),
    task("remote-task", ["remote"]),
    task("shared", ["first", "second", "remote"]),
    task("scope-missing", ["first"], [], [second.id]),
  ];

  for (const [actor, taken] of [
    [first, ["first-task", "shared"]],
    [second, ["second-task", "shared"]],
    [null, []],
  ]) {
    assert.deepEqual(laneIds(tasks, actor), {
      Available: [],
      Taken: taken,
      Ongoing: tasks.map(({task_id}) => task_id),
    });
  }
});
