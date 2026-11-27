import { randomUUID } from 'node:crypto';

const sessionId='local-mcp';
const uuid=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const pageSize=30;
const ready='ready';
const unsupported={state:'unsupported'};
const failureStates=new Set(['unauthorized','wrong_credentials','rate_limited','profile_unavailable',
  'not_registered','forbidden','not_found','board_ack_required','conflict','member_retired',
  'membership_changed','room_archived','room_has_live_children','invalid_peer','invalid_request','unavailable']);
const roomFields=['history_policy','parent_room_id','created_at','expires_at','archived_at','archive_reason',
  'leader_member_id','leadership_epoch'];
const membershipId=/^[0-9a-f]{32}$/;
const timestamp=/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/;
const text=(value,limit)=>typeof value==='string'&&value.trim().length>0&&value.length<=limit;
const optionalText=(value,limit)=>value===undefined||text(value,limit);
const focusedTools={
  '/api/room-leadership':{read:'teamup_room_leadership_candidates',write:'teamup_handoff_room_leadership',
    keys:['leadership_epoch','mode','member_id','membership_id','leave'],valid:body=>
      ['choose','auto','defer'].includes(body.mode)&&Number.isSafeInteger(body.leadership_epoch)&&body.leadership_epoch>=1
      &&(body.mode==='choose'?uuid.test(body.member_id)&&membershipId.test(body.membership_id)
        :body.member_id===undefined&&body.membership_id===undefined)
      &&(body.leave===undefined||typeof body.leave==='boolean')},
  '/api/child-rooms':{read:'teamup_list_child_rooms',write:'teamup_create_linked_room',parent:true,paged:true,
    keys:['name','rules','protocol','history_policy','client_request_id'],valid:body=>
      text(body.name,200)&&text(body.client_request_id,128)&&optionalText(body.rules,8000)
      &&optionalText(body.protocol,8000)&&['since_joining','retained'].includes(body.history_policy??'since_joining')},
  '/api/room-invitations':{write:'teamup_invite_to_room',keys:['member_id','client_request_id'],valid:body=>
    uuid.test(body.member_id)&&text(body.client_request_id,128)},
  '/api/room-lease':{write:'teamup_extend_room_lease',keys:['seconds','expected_expires_at'],valid:body=>
    Number.isInteger(body.seconds)&&body.seconds>=1&&body.seconds<=3600
    &&typeof body.expected_expires_at==='string'&&timestamp.test(body.expected_expires_at)},
};
const attempt=async run=>{
  try {
    return {state:ready,...await run()};
  } catch(error) {
    if(error.toolUnavailable)return unsupported;
    if(failureStates.has(error.code))return {state:error.code};
    throw error;
  }
};
const metadata=(source,fields)=>Object.fromEntries(fields.filter((field)=>source[field]!==undefined&&source[field]!==null)
  .map((field)=>[field,source[field]]));
const membershipState=(room,memberId)=>({
  ...metadata(room,['membership_id']),
  ...(room.can_remove_members===true||room.leader_member_id===memberId?{can_remove_members:true}:{}),
});
const cursor=(value,fallback)=>{
  if(value===null)return fallback;
  const n=Number(value);
  if(!Number.isSafeInteger(n)||n<0)throw new Error('Invalid cursor');
  return n;
};
const boardState=room=>({current:room.current_board_version,acknowledged:room.acked_board_version});
function memberView(member) {
  return {id:member.member_id,role:member.role_description,name:member.name||member.display_name,
    owner_username:member.owner_username,description:member.description,presence:{state:'unknown',observed_at:null,age_ms:null}};
}
function messageView(message, members) {
  const recipients=message.mention_member_ids??(message.mention_member_id?[message.mention_member_id]:[]);
  return {id:String(message.message_id),sender:members.get(message.sender_member_id)??message.sender_member_id.slice(0,8),
    sender_member_id:message.sender_member_id,text:message.text,created_at:message.created_at,
    mentioned:recipients.length ? members.get(recipients[0])??recipients[0].slice(0,8) : null,
    mentioned_recipients:recipients.map(id=>members.get(id)??id.slice(0,8)),recipient_member_ids:recipients};
}
export function makeConsoleApi(mcp, { memberId }) {
  let snapshot, nextRead=0, pending;
  let rosterUnsupportedUntil=0;
  async function catalog() {
    if(snapshot&&Date.now()<nextRead)return snapshot;
    if(!pending)pending=mcp([['teamup_list_rooms',{}],['teamup_find_members',{}]]).then(([rooms,members])=>{
      nextRead=Date.now()+4500;
      snapshot={rooms:rooms.rooms,members:members.members};
      return snapshot;
    }).finally(()=>{pending=null;});
    return pending;
  }
  const call=async(name,args)=>(await mcp([[name,args]]))[0];
  const progressIds=new Map();
  return async (method,url,readBody) => {
    const path=url.pathname;
    const params=url.searchParams;
    if(method==='GET'&&path==='/api/versions')return {client_version:'Windows · UI v0.2.1',server_version:null,server_state:'unknown'};
    if(method==='GET'&&path==='/api/retired')return unsupported;
    const {rooms,members}=await catalog();
    if(method==='GET'&&path==='/api/workspace') {
      const me=members.find(m=>m.member_id===memberId);
      return {sessions:[{id:sessionId,label:me?.name||me?.role_description||'TeamUp member',harness:'Codex · SSH',state:ready,member_id:memberId,
          role_description:me?.role_description}],rooms:rooms.map(r=>({id:r.room_id,name:r.name,
          latest_message_id:r.latest_message_id==null?null:String(r.latest_message_id),
          memberships:[{session:sessionId,...boardState(r),active_task:null,...membershipState(r,memberId)}],
          ...metadata(r,roomFields)})),directory_available:false,retired_sessions:[]};
    }
    if(params.has('session')&&params.get('session')!==sessionId)return {state:'not_found'};
    if(method==='GET'&&path==='/api/team')return {state:ready,members:members.map(memberView),presence_as_of:null};
    const roomId=params.get('room');
    const room=rooms.find(r=>r.room_id===roomId);
    if(!room)return {state:'not_found'};
    if(method==='GET'&&path==='/api/room-members') {
      if(Date.now()>=rosterUnsupportedUntil) {
        try {
          const value=await call('teamup_list_room_members',{room_id:roomId});
          return {state:ready,current:room.current_board_version,history_policy:room.history_policy,
            members:value.members.map(m=>({...memberView(m),acknowledged:m.acked_board_version,
              membership_id:m.membership_id,tasks:null}))};
        } catch(error) {
          if(error.toolUnavailable) rosterUnsupportedUntil=Date.now()+5*60*1000;
          else if(failureStates.has(error.code)) return {state:error.code,members:[]};
          else throw error;
        }
      }
      return {state:'directory_only',members:members.map(memberView)};
    }
    if(method==='GET'&&path==='/api/room-board') {
      const value=await call('teamup_get_board',{room_id:roomId});
      return {state:ready,board:{...value,can_edit:false}};
    }
    if(method==='GET'&&path==='/api/room') {
      const after=cursor(params.get('after'),null);
      const before=cursor(params.get('before'),null);
      const pagination=after!==null?{after}:before!==null?{before}:room.latest_message_id==null?{after:0}:{before:room.latest_message_id+1};
      if(room.acked_board_version!==room.current_board_version)return {state:'board_ack_required',board:boardState(room),messages:[],older:null};
      const history=await call('teamup_read_messages',{room_id:roomId,limit:pageSize,...pagination});
      const profiles=new Map(members.map(m=>[m.member_id,m.name||m.display_name||m.role_description]));
      const messages=history.messages.map(m=>messageView(m,profiles));
      return {state:ready,board:boardState(room),messages,
        older:'before' in pagination&&messages.length===pageSize?messages[0].id:null,
        more_recent:'after' in pagination&&messages.length===pageSize,
        history_truncated:history.history_truncated,expired_through_message_id:history.expired_through_message_id==null?
          undefined:String(history.expired_through_message_id)};
    }
    if(method==='GET'&&path==='/api/tasks') {
      const status=params.get('status');
      if(status&&!['open','finished'].includes(status))return {state:'invalid_request'};
      const data=await call('teamup_list_tasks',{room_id:roomId,...(status?{status}:{}),after:cursor(params.get('after'),0),limit:pageSize});
      return {state:ready,tasks:data.tasks.map(task=>({...task,task_number:String(task.task_number),visible_to:[sessionId]})),
        next_after:data.next_after==null?null:String(data.next_after)};
    }
    if(method==='GET'&&path==='/api/task-events') {
      const id=params.get('task');
      if(!uuid.test(id))return {state:'invalid_request'};
      const data=await call('teamup_task_history',{room_id:roomId,task_id:id,after:cursor(params.get('after'),0),limit:pageSize});
      return {state:ready,events:data.events,next_after:data.next_after==null?null:String(data.next_after)};
    }
    const focused=focusedTools[path];
    if(focused) {
      if(params.get('session')!==sessionId)return {state:'not_found'};
      const after=params.get('after');
      if(method==='GET'&&focused.read) {
        if(after!==null&&(!focused.paged||!uuid.test(after)))return {state:'invalid_request'};
        return attempt(()=>call(focused.read,{room_id:roomId,...(after?{after}:{})}));
      }
      if(method==='POST'&&focused.write) {
        const body=await readBody();
        if(!body||typeof body!=='object'||Array.isArray(body)||!focused.valid(body))return {state:'invalid_request'};
        const args={[focused.parent?'parent_room_id':'room_id']:roomId};
        for(const key of focused.keys)if(body[key]!==undefined)args[key]=body[key];
        return attempt(()=>call(focused.write,args));
      }
      return unsupported;
    }
    const action=/^\/api\/tasks\/([^/]+)\/(join|progress|finish)$/.exec(path);
    if(method==='POST'&&action&&uuid.test(action[1])&&params.get('session')===sessionId) {
      const [,taskId,kind]=action;
      const args={room_id:roomId,task_id:taskId};
      let key;
      if(kind==='progress') {
        const {note}=await readBody();
        if(typeof note!=='string'||!note.trim()||note.length>8000)return {state:'invalid_request'};
        key=JSON.stringify([roomId,taskId,note]);
        if(!progressIds.has(key))progressIds.set(key,randomUUID());
        Object.assign(args,{note,client_progress_id:progressIds.get(key)});
      }
      const result=await call({join:'teamup_join_task',progress:'teamup_report_progress',finish:'teamup_finish_task'}[kind],args);
      if(key)progressIds.delete(key);
      return {state:ready,...result};
    }
    return unsupported;
  };
}
