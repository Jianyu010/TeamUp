'use strict';
const credential=location.hash.slice(1)||sessionStorage.getItem('teamup-local');
if (location.hash) {
  sessionStorage.setItem('teamup-local',credential);
  history.replaceState(null,'',location.pathname);
}
const at=id=>document.getElementById(id);
const roomNames=new Map(), memberNames=new Map();
let refreshing=false, last;
async function request(path, data) {
  const headers={'x-teamup-local':credential||''};
  const response=await fetch(path,data===undefined ? {headers} : {
    method:'POST',headers:{...headers,'content-type':'application/json'},body:JSON.stringify(data),
  });
  const result=await response.json();
  if(!response.ok) throw new Error(result.error);
  return result;
}
function options(selector, list) {
  const field=at(selector), selected=field.value;
  field.replaceChildren(new Option('请选择',''),...list.map(([value,label])=>new Option(label,value)));
  if (list.some(([value])=>value===selected)) field.value=selected;
}
function el(tag,value,className) {
  const node=document.createElement(tag);
  node.textContent=value;
  if(className) node.className=className;
  return node;
}
function button(label,fn) {
  const control=el('button',label);
  control.onclick=async()=>{
    control.disabled=true;
    try { await fn(); } catch(error) { at('status').textContent=error.message; }
    finally { control.disabled=false; }
  };
  return control;
}
function senderLabel(sender) { return memberNames.get(sender)||sender; }
function render(data) {
  for(const room of data.rooms) roomNames.set(room.room_id,room.name);
  for(const member of data.members) memberNames.set(member.member_id,member.display_name||member.name||member.role_description||member.member_id);
  options('room',[...roomNames]);
  options('sender',[...memberNames].map(([id,name])=>[id,`${name} · ${id.slice(0,8)}`]));
  if(data.rule) {
    if(!at('thread').value) at('thread').value=data.rule.thread;
    if(!at('room').value) at('room').value=data.rule.room;
    if(!at('sender').value) at('sender').value=data.rule.sender;
  }
  at('rule').textContent=data.rule?`自动投递已启用 · ${roomNames.get(data.rule.room)||data.rule.room} · ${senderLabel(data.rule.sender)}`:'自动投递已关闭';
  at('status').textContent=data.error||'已连接 · '+new Date().toLocaleTimeString();
  at('notices').replaceChildren(...data.recent.map(notice=>{
    const card=el('article',''), head=el('div','','notice-head'), actions=el('div','','actions');
    const preview=el('pre',''); preview.hidden=true;
    const verdict=el('span',data.delivery[notice.event_id+':'+at('thread').value]||'','muted');
    head.append(el('strong',senderLabel(notice.sender_member_id)),
      el('span',`${roomNames.get(notice.room_id)||'房间'} · #${notice.message_id}`,'muted'));
    actions.append(button('查看消息',async()=>{
      if(!preview.hidden){preview.hidden=true;return;}
      const message=await request('/message',{notice});
      preview.textContent=message.text;
      preview.hidden=false;
    }),button('投递到所选会话',async()=>{
      if(!at('thread').value) throw new Error('请先选择目标 Codex 会话');
      verdict.textContent=(await request('/deliver',{event:notice.event_id,thread:at('thread').value})).result;
    }),verdict);
    card.append(head,preview,actions);
    return card;
  }));
}
async function refresh() {
  if(refreshing)return;
  refreshing=true;
  try {
    const inbox=await request('/inbox');
    last=inbox;
    render(inbox);
  } catch(error) { at('status').textContent=error.message; }
  finally { refreshing=false; }
}
at('refresh').onclick=refresh;
at('workspace').onclick=()=>{
  if(credential)sessionStorage.setItem('teamup-console',credential);
  location.assign('/desktop');
};
async function setRule(enabled) {
  try {
    const result=await request('/rule',{enabled,thread:at('thread').value,room:at('room').value,sender:at('sender').value});
    if(last){last.rule=result.rule;render(last);}
    await refresh();
  } catch(error) { at('status').textContent=error.message; }
}
at('enable').onclick=()=>setRule(true);
at('disable').onclick=()=>setRule(false);
refresh();
