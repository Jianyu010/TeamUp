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

const child='40000000-0000-0000-0000-000000000000';
const lease={parent_room_id:room,created_at:'2026-11-28T19:00:00Z',expires_at:'2026-11-29T00:00:00Z'};

function focusedEndpoint(rooms, answer) {
  const calls=[];
  const api=makeConsoleApi(async queries=>queries.map(([tool,args])=>{
    calls.push([tool,args]);
    if(tool==='teamup_list_rooms')return {rooms};
    if(tool==='teamup_find_members')return {members:directory};
    return answer?answer(tool,args):{room_id:room};
  }),{memberId:me});
  return {api,calls,scope:'?session=local-mcp&room='+room};
}

test('publishes room leadership and lease metadata with the local membership',async()=>{
  const foreign='50000000-0000-0000-0000-000000000000';
  const {api}=focusedEndpoint([
    {room_id:room,name:'Team chat',current_board_version:2,acked_board_version:1,membership_id:'a'.repeat(32),
      leader_member_id:me,leadership_epoch:7,...lease},
    {room_id:child,name:'Other lead',current_board_version:1,acked_board_version:1,membership_id:'b'.repeat(32),
      leader_member_id:foreign,leadership_epoch:3},
  ]);
  const {rooms:[led,other]}=await api('GET',url('/api/workspace'));
  assert.deepEqual(led,{id:room,name:'Team chat',latest_message_id:null,
    memberships:[{session:'local-mcp',current:2,acknowledged:1,active_task:null,membership_id:'a'.repeat(32),can_remove_members:true}],
    parent_room_id:room,created_at:lease.created_at,expires_at:lease.expires_at,leader_member_id:me,leadership_epoch:7});
  assert.deepEqual(other.memberships,[{session:'local-mcp',current:1,acknowledged:1,active_task:null,membership_id:'b'.repeat(32)}]);
  assert.equal(other.leader_member_id,foreign);
  assert.equal('can_remove_members' in other,false);
});

test('lists and creates child rooms through the selected MCP binding',async()=>{
  const {api,calls,scope}=focusedEndpoint([{room_id:room,name:'Team chat'}],(tool,args)=>tool==='teamup_list_child_rooms'
    ?{rooms:[{room_id:child,name:'Investigation',history_policy:'since_joining',...lease}],next_after:null}:{...lease,room_id:child});
  const list='/api/child-rooms'+scope;
  assert.deepEqual(await api('GET',url(list+'&after='+child)),{state:'ready',rooms:[{room_id:child,name:'Investigation',history_policy:'since_joining',...lease}],next_after:null});
  assert.equal((await api('GET',url(list+'&after=not-a-room'))).state,'invalid_request');
  const body={name:'Investigation',client_request_id:'new-child',rules:'Stay focused.',history_policy:'retained'};
  const created=await api('POST',url(list),async()=>body);
  assert.equal(created.state,'ready');
  assert.equal(created.room_id,child);
  assert.deepEqual(calls.slice(-2),[
    ['teamup_list_child_rooms',{room_id:room,after:child}],
    ['teamup_create_linked_room',{parent_room_id:room,...body}],
  ]);
  assert.equal((await api('POST',url(list),async()=>({name:' ',client_request_id:'x'}))).state,'invalid_request');
  assert.equal((await api('POST',url(list),async()=>({name:'Investigation',rules:''}))).state,'invalid_request');
  assert.equal((await api('POST',url(list),async()=>({name:'Investigation',client_request_id:'x',history_policy:'open'}))).state,'invalid_request');
  assert.equal(calls.length,4);
});

test('invites members and renews leases through the selected MCP binding',async()=>{
  const expires=lease.expires_at;
  const {api,calls,scope}=focusedEndpoint([{room_id:room,name:'Team chat'}],(tool)=>tool==='teamup_invite_to_room'
    ?{dm_id:'dm',message_id:9}:{room_id:room,...lease,expires_at:'2026-11-29T01:00:00Z'});
  assert.equal((await api('GET',url('/api/room-invitations'+scope))).state,'unsupported');
  const invitation={member_id:teammate,client_request_id:'invite'};
  assert.deepEqual(await api('POST',url('/api/room-invitations'+scope),async()=>invitation),{state:'ready',dm_id:'dm',message_id:9});
  assert.equal((await api('POST',url('/api/room-invitations'+scope),async()=>({member_id:'lead',client_request_id:'invite'}))).state,'invalid_request');
  const renewal={seconds:1800,expected_expires_at:expires};
  assert.deepEqual(await api('POST',url('/api/room-lease'+scope),async()=>renewal),
    {state:'ready',room_id:room,...lease,expires_at:'2026-11-29T01:00:00Z'});
  assert.equal((await api('POST',url('/api/room-lease'+scope),async()=>({seconds:3601,expected_expires_at:expires}))).state,'invalid_request');
  assert.equal((await api('POST',url('/api/room-lease'+scope),async()=>({seconds:600,expected_expires_at:'2026-11-29'}))).state,'invalid_request');
  assert.equal((await api('POST',url('/api/room-leadership'+scope),async()=>({mode:'auto',leadership_epoch:7,member_id:teammate}))).state,'invalid_request');
  assert.deepEqual(calls.slice(-2),[
    ['teamup_invite_to_room',{room_id:room,...invitation}],
    ['teamup_extend_room_lease',{room_id:room,...renewal}],
  ]);
});

test('the roster reports a published failure and keeps its fallback for a missing tool',async()=>{
  let failure=Object.assign(new Error('teamup_list_room_members 查询失败'),{code:'forbidden'});
  const api=endpoint(()=>{throw failure;});
  assert.deepEqual(await api('GET',url(roomPath)),{state:'forbidden',members:[]});
  failure=Object.assign(new Error('teamup_list_room_members 查询失败'),
    {code:'invalid_request',toolUnavailable:true});
  assert.deepEqual((await api('GET',url(roomPath))).state,'directory_only');
});

test('keeps published failure codes and requires the selected actor for focused routes',async()=>{
  let failure={code:'conflict'};
  const {api,calls,scope}=focusedEndpoint([{room_id:room,name:'Team chat'}],()=>{throw Object.assign(new Error('teamup_handoff_room_leadership 查询失败'),failure);});
  assert.deepEqual(await api('POST',url('/api/room-leadership'+scope),async()=>({mode:'defer',leadership_epoch:7,leave:true})),{state:'conflict'});
  failure={code:'forbidden'};
  assert.deepEqual(await api('GET',url('/api/room-leadership'+scope)),{state:'forbidden'});
  failure={toolUnavailable:true};
  assert.deepEqual(await api('GET',url('/api/child-rooms'+scope)),{state:'unsupported'});
  failure={};
  await assert.rejects(api('GET',url('/api/room-leadership'+scope)),/查询失败/);
  const before=calls.length;
  assert.deepEqual(await api('GET',url('/api/room-leadership?session=other&room='+room)),{state:'not_found'});
  assert.deepEqual(await api('GET',url('/api/room-leadership?room='+room)),{state:'not_found'});
  assert.equal(calls.length,before);
});
