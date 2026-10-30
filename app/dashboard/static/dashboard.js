'use strict';
const $ = id => document.getElementById(id);
let events = [], cursor = null;
const node = (tag, text, cls) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
async function api(path, options={}) {
  const response=await fetch(path,{credentials:'same-origin',...options});
  if(!response.ok){if(response.status===401){$('login').hidden=false;$('workspace').hidden=true;$('logout').hidden=true;events=[];$('sessions').replaceChildren();}throw new Error((await response.json().catch(()=>({}))).detail||'Request failed.');}
  return response.json();
}
function message(error){$('message').textContent=error.message||String(error);}
function turn(row){
  const el=node('div',undefined,'turn'), d=row.details;
  el.append(node('h3',new Date(row.timestamp).toLocaleString()+' · '+row.kind));
  if(d.outcome)el.append(node('p','Recorded outcome: '+d.outcome.replaceAll('_',' ')));
  for(const label of d.labels||[])el.append(node('span',label.label,'tag'));
  if(d.likely_no_legal_problem?.value)el.append(node('p','No legal problem identified by the classifier.'));
  if(d.query)el.append(node('p',d.query));
  for(const qa of d.followup_answers||[])el.append(node('p','Question: '+qa.question+'\nAnswer: '+qa.answer));
  for(const q of d.follow_up_questions||[])el.append(node('p','Generated question: '+q.question+(q.options?.length?'\nOptions: '+q.options.join(' · '):'')));
  if(row.kind==='classification'&&!d.content_captured)el.append(node('p','Conversation text was not retained.'));
  return el;
}
async function history(session, target){
  let next=null;target.replaceChildren(node('p','Loading conversation…'));
  const rows=[];const button=node('button','Load earlier turns');
  async function load(){button.disabled=true;try{
    const query=new URLSearchParams({days:$('days').value,session});if(next)query.set('cursor',next);
    const page=await api('/dashboard/api/activity?'+query);rows.push(...page.events);next=page.next_cursor;
    target.replaceChildren(...[...rows].sort((a,b)=>a.sort_key.localeCompare(b.sort_key)).map(turn));
    if(next)target.append(button);
    if(!rows.length)target.append(node('p','No retained records in this period.'));
  }catch(e){target.replaceChildren(node('p',e.message));}finally{button.disabled=false;}}
  button.onclick=load;await load();
}
function render(){
  const groups=new Map();for(const row of events){if(!groups.has(row.conversation_id))groups.set(row.conversation_id,[]);groups.get(row.conversation_id).push(row);}
  $('sessions-count').textContent=groups.size;$('events-count').textContent=events.length;
  $('outcomes-count').textContent=[...groups.values()].filter(rows=>rows.some(x=>x.kind==='outcome')).length;
  $('sessions').replaceChildren();const search=$('search').value.toLowerCase();
  for(const [id,rows]of groups){if(!JSON.stringify([id,rows.map(x=>x.details.labels)]).toLowerCase().includes(search))continue;
    const card=node('details',undefined,'session'), summary=node('summary',id), content=node('div');
    const latest=rows.find(x=>x.kind==='classification');const outcome=rows.find(x=>x.kind==='outcome');
    summary.append(node('p',new Date(rows[0].timestamp).toLocaleString()+' · '+(outcome?outcome.details.outcome.replaceAll('_',' '):'No completion recorded')));
    for(const label of latest?.details.labels||[])summary.append(node('span',label.label,'tag'));
    card.append(summary,content);let loaded=false;card.addEventListener('toggle',()=>{if(card.open&&!loaded){loaded=true;history(id,content);}});$('sessions').append(card);
  }
  if(!groups.size)$('sessions').append(node('p','No activity recorded in this period.'));
  $('more').hidden=!cursor;
}
async function load(reset=true){
  $('message').textContent='';$('more').disabled=true;
  try{const q=new URLSearchParams({days:$('days').value});if(!reset&&cursor)q.set('cursor',cursor);
    const data=await api('/dashboard/api/activity?'+q);events=reset?data.events:[...events,...data.events];
    events=[...new Map(events.map(x=>[x.sort_key,x])).values()];cursor=data.next_cursor;
    $('client').textContent=data.client_id;$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
    $('export').href='/dashboard/api/export?days='+$('days').value;render();
  }catch(e){message(e);}finally{$('more').disabled=false;}
}
$('login').onsubmit=async e=>{e.preventDefault();try{await api('/dashboard/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:$('token').value})});$('token').value='';await load();}catch(error){message(error);}};
$('logout').onclick=async()=>{try{await api('/dashboard/logout',{method:'POST'});events=[];cursor=null;$('sessions').replaceChildren();$('workspace').hidden=true;$('logout').hidden=true;$('login').hidden=false;$('message').textContent='Signed out.';}catch(e){message(e);}};
$('refresh').onclick=()=>load();$('days').onchange=()=>load();$('more').onclick=()=>load(false);$('search').oninput=render;
load();
