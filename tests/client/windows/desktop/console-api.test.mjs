import assert from 'node:assert/strict';
import test from 'node:test';
import { makeConsoleApi } from '../../../../client/windows/desktop/console-api.mjs';

const room='10000000-0000-0000-0000-000000000000';
const me='20000000-0000-0000-0000-000000000000';
const teammate='30000000-0000-0000-0000-000000000000';
const directory=[
  {member_id:me,role_description:'Client work'},
  {member_id:teammate,role_description:'Release work'},
];
const url=path=>new URL(path,'http://127.0.0.1');
const roomPath='/api/room-members?session=local-mcp&room='+room;

function endpoint(getRoster) {
  return makeConsoleApi(async calls=>calls.map(([tool,args])=>{
    switch(tool) {
      case 'teamup_list_rooms':
        return {rooms:[{room_id:room,name:'Team chat',current_board_version:1,acked_board_version:1}]};
      case 'teamup_find_members':
        return {members:directory};
      case 'teamup_list_room_members':
        assert.equal(args.room_id,room);
        return getRoster();
      default: throw new Error('Unexpected MCP call: '+tool);
    }
  }),{memberId:me});
}

test('uses current room membership instead of the team directory when available',async()=>{
  const api=endpoint(()=>({members:[{...directory[0],acked_board_version:1,membership_id:'member-in-room'}]}));
  const result=await api('GET',url(roomPath));
  assert.equal(result.state,'ready');
  assert.deepEqual(result.members.map(m=>[m.id,m.membership_id]),[[me,'member-in-room']]);
  assert.equal((await api('GET',url('/api/team?session=local-mcp'))).members.length,2);
});

test('labels the fallback and never turns team directory entries into room memberships',async()=>{
  let requests=0;
  const api=endpoint(()=>{
    requests++;
    throw Object.assign(new Error('Unknown TeamUp tool'),{toolUnavailable:true});
  });
  const result=await api('GET',url(roomPath));
  assert.equal(result.state,'directory_only');
  assert.deepEqual(result.members.map(m=>m.id),[me,teammate]);
  assert.ok(result.members.every(m=>!m.membership_id));
  assert.equal((await api('GET',url(roomPath))).state,'directory_only');
  assert.equal(requests,1);
});

test('does not recast authorization or connection errors as an unsupported capability',async()=>{
  const api=endpoint(()=>{throw new Error('unauthorized');});
  await assert.rejects(api('GET',url(roomPath)),/unauthorized/);
  assert.deepEqual(await api('GET',url(roomPath.replace('local-mcp','unknown'))),{state:'not_found'});
});

test('routes focused actions through the selected MCP binding',async()=>{
  const calls=[];
  const api=makeConsoleApi(async queries=>queries.map(([tool,args])=>{
    calls.push([tool,args]);
    switch(tool) {
      case 'teamup_list_rooms':
        return {rooms:[{room_id:room,name:'Team chat',leader_member_id:me,leadership_epoch:7}]};
      case 'teamup_find_members':
        return {members:directory};
      case 'teamup_room_leadership_candidates':
        return {candidates:[],leadership_epoch:7};
      case 'teamup_handoff_room_leadership':
        return {left:false,pending:true};
      default: throw new Error('Unexpected MCP call: '+tool);
    }
  }),{memberId:me});
  const endpoint='/api/room-leadership?session=local-mcp&room='+room;
  assert.equal((await api('GET',url('/api/workspace'))).rooms[0].leadership_epoch,7);
  assert.deepEqual(await api('GET',url(endpoint)),{state:'ready',candidates:[],leadership_epoch:7});
  const body={mode:'defer',leadership_epoch:7,leave:true};
  assert.deepEqual(await api('POST',url(endpoint),async()=>body),{state:'ready',left:false,pending:true});
  assert.deepEqual(calls.slice(-2),[
    ['teamup_room_leadership_candidates',{room_id:room}],
    ['teamup_handoff_room_leadership',{room_id:room,...body}],
  ]);
});
