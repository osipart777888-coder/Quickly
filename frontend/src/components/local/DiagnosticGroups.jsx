import {useMemo,useState} from 'react';
import {api} from '../../api';
import {translate} from '../../i18n';

export default function DiagnosticGroups({accounts,onChanged}){
  const [selected,setSelected]=useState(new Set()),[search,setSearch]=useState(''),[provider,setProvider]=useState('');
  const [preview,setPreview]=useState(null),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const [checks,setChecks]=useState(1),[autoReply,setAutoReply]=useState(false),[budget,setBudget]=useState('');
  const visible=useMemo(()=>accounts.filter(a=>(!provider||a.provider===provider)&&`${a.email} ${a.display_name||''}`.toLowerCase().includes(search.toLowerCase())),[accounts,search,provider]);
  const ids=accounts.filter(a=>selected.has(a.id)).map(a=>a.id);
  function changeSelection(values){setSelected(new Set(values));setPreview(null);}
  async function inspect(){setBusy(true);setMessage('');try{setPreview(await api.post('/local/diagnostics/group-preview',{inbox_ids:ids}));}catch(e){setMessage(e.message);}finally{setBusy(false);}}
  async function save(e){e.preventDefault();const form=e.currentTarget;const f=new FormData(form);setBusy(true);setMessage('');try{
    await api.post('/local/diagnostics/plans',{mode:'group',name:f.get('name'),inbox_ids:ids,owned_accounts_confirmed:f.has('owned'),enabled:false,
      interval_minutes:Number(f.get('interval')),per_account_daily_limit:checks,max_messages_per_day:Number(f.get('budget')),auto_reply:autoReply,
      sending_days:f.getAll('day').map(Number),hours_start:f.get('start'),hours_end:f.get('end')});
    setMessage(translate('Group saved in paused state. Enable its schedule below to start checks.'));await onChanged();
  }catch(err){setMessage(err.message);}finally{setBusy(false);}}
  return <section className="rounded-xl border bg-white p-5 space-y-4"><div><h2 className="font-semibold text-lg">Automatic group diagnostics</h2><p className="text-sm text-gray-600 mt-2">Select your mailboxes once. Each cycle queues one outgoing check for every available member; recipients rotate between cycles. Sending is paced through a persistent queue.</p></div>
    {!accounts.length&&<p className="rounded border border-blue-100 bg-blue-50 p-3 text-sm">Add your sending accounts first. A group can contain up to 2000 mailboxes.</p>}
    <div className="flex flex-wrap gap-3"><label className="text-sm">Search accounts<input value={search} onChange={e=>setSearch(e.target.value)} className="block rounded border"/></label><label className="text-sm">Provider<select value={provider} onChange={e=>setProvider(e.target.value)} className="block rounded border"><option value="">All providers</option><option value="smtp">SMTP / IMAP</option><option value="gmail">Google OAuth</option><option value="office365">Microsoft OAuth</option></select></label></div>
    <div className="flex flex-wrap gap-2"><button type="button" disabled={!accounts.length||busy} className="rounded border px-3 py-2" onClick={()=>changeSelection(accounts.map(a=>a.id))}>Select all accounts</button><button type="button" disabled={!visible.length||busy} className="rounded border px-3 py-2" onClick={()=>changeSelection([...ids,...visible.map(a=>a.id)])}>Select matching accounts</button><button type="button" disabled={!ids.length||busy} className="rounded border px-3 py-2" onClick={()=>changeSelection([])}>Clear selection</button><span className="self-center text-sm">Selected accounts: {ids.length} / {accounts.length}</span></div>
    {accounts.length>0&&<div className="max-h-64 overflow-y-auto rounded border p-3 grid gap-2 md:grid-cols-2">{visible.map(a=><label key={a.id} className="flex items-start gap-2 text-sm break-all"><input type="checkbox" checked={selected.has(a.id)} onChange={e=>changeSelection(e.target.checked?[...ids,a.id]:ids.filter(id=>id!==a.id))} className="mt-1 rounded"/><span>{a.email}{a.paused&&<span className="block text-xs text-amber-700">Paused — excluded until resumed</span>}</span></label>)}{!visible.length&&<p>No matching accounts.</p>}</div>}
    <button type="button" disabled={busy||ids.length<2} className="rounded border px-3 py-2 disabled:opacity-50" onClick={inspect}>Preview group allocation</button>
    {preview&&<div className="rounded border p-3 text-sm space-y-2"><p>Available accounts: {preview.eligible} / {preview.selected}</p><p>Outgoing checks per cycle: {preview.messages_per_cycle}</p><p>Preview does not send messages. Quotas and connection status are checked again before each send.</p><details><summary className="cursor-pointer">Show allocation and exclusions</summary><div className="max-h-64 overflow-auto mt-2">{preview.pairs.map(p=><p key={p.sender_id} className="font-mono text-xs py-1">{p.sender} → {p.recipient}</p>)}{preview.members.filter(m=>!m.eligible).map(m=><p key={m.id} className="text-amber-700 py-1">{m.email||m.id}: {m.reason}</p>)}</div></details></div>}
    <form onSubmit={save} className="space-y-4"><div className="grid gap-3 md:grid-cols-2"><label>Group name<input name="name" required defaultValue={translate('My mailbox group')} className="block rounded border w-full"/></label><label>Repeat full cycle, minutes<input name="interval" type="number" defaultValue="1440" min="15" max="43200" required className="block rounded border w-full"/></label>
      <label>Outgoing checks per mailbox per day<input type="number" value={checks} onChange={e=>setChecks(Number(e.target.value))} min="1" max="100" required className="block rounded border w-full"/></label>
      <label>Total diagnostic messages per group per day<input name="budget" type="number" value={budget||Math.max(1,ids.length*checks*(autoReply?2:1))} onChange={e=>setBudget(e.target.value)} min="1" max="100000" required className="block rounded border w-full"/></label>
      <label>Start time (Moscow)<input name="start" type="time" defaultValue="09:00" required className="block rounded border w-full"/></label><label>End time (Moscow)<input name="end" type="time" defaultValue="18:00" required className="block rounded border w-full"/></label></div>
      <fieldset><legend>Schedule days</legend><div className="flex flex-wrap gap-3 mt-2">{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((d,i)=><label key={d}><input name="day" type="checkbox" value={i} defaultChecked={i<5} className="rounded mr-1"/>{d}</label>)}</div></fieldset>
      <label className="flex gap-2 items-start text-sm"><input type="checkbox" checked={autoReply} onChange={e=>setAutoReply(e.target.checked)} className="mt-1 rounded"/><span>Automatically send a labelled diagnostic reply after confirmed receipt</span></label>
      <p className="text-xs text-gray-600">Replies use the group message budget and the replying mailbox daily quota. The outgoing-check limit counts initial probes only. Emergency stop, pauses and normal mailbox limits apply to every message.</p>
      <label className="flex gap-2 items-start text-sm"><input name="owned" type="checkbox" required className="mt-1 rounded"/><span>All selected mailboxes belong to me and I permit this diagnostic exchange.</span></label>
      <button disabled={busy||ids.length<2} className="rounded bg-blue-700 px-4 py-2 text-white disabled:opacity-50">Save group in paused state</button>
    </form>{message&&<p role="status" className="whitespace-pre-wrap text-sm">{message}</p>}
  </section>;
}

export function GroupSchedule({plan,onAction,busy}){
  const c=plan.cycle_counts||{};
  const [editing,setEditing]=useState(false);
  function save(e){e.preventDefault();const f=new FormData(e.currentTarget);onAction(async()=>{await api.put(`/local/diagnostics/plans/${plan.id}/settings`,{name:f.get('name'),interval_minutes:Number(f.get('interval')),per_account_daily_limit:Number(f.get('checks')),max_messages_per_day:Number(f.get('budget')),auto_reply:f.has('autoReply'),sending_days:f.getAll('day').map(Number),hours_start:f.get('start'),hours_end:f.get('end')});setEditing(false);});}
  return <div className="border-t py-4 space-y-3"><div className="flex flex-wrap justify-between gap-2"><div><h3 className="font-semibold">{plan.name}</h3><p className="text-sm">Group accounts: {plan.inbox_ids.length} · {plan.enabled?'Enabled':'Paused'}</p><p className="text-xs text-gray-500">{plan.interval_minutes} min · {plan.hours_start}–{plan.hours_end} MSK</p></div><button disabled={busy} className="rounded border px-3" onClick={()=>onAction(()=>api.put(`/local/diagnostics/plans/${plan.id}`,{enabled:!plan.enabled}))}>{plan.enabled?'Stop schedule':'Enable schedule'}</button></div>
    <p className="text-sm">Outgoing checks per mailbox per day: {plan.per_account_daily_limit} · Diagnostic replies: {plan.auto_reply?'Enabled':'Disabled'}</p>
    <button type="button" disabled={busy} className="rounded border px-3 py-2 text-sm" onClick={()=>setEditing(!editing)}>{editing?'Cancel editing':'Edit group settings'}</button>
    {editing&&<form onSubmit={save} className="rounded border p-3 space-y-3">
      <p className="text-sm">Saving pauses this group and preserves its queue and used quotas. Enable the schedule again when ready.</p>
      <div className="grid gap-3 md:grid-cols-2">
        <label>Group name<input name="name" required defaultValue={plan.name} className="block w-full rounded border"/></label>
        <label>Repeat full cycle, minutes<input name="interval" type="number" min="15" max="43200" required defaultValue={plan.interval_minutes} className="block w-full rounded border"/></label>
        <label>Outgoing checks per mailbox per day<input name="checks" type="number" min="1" max="100" required defaultValue={plan.per_account_daily_limit} className="block w-full rounded border"/></label>
        <label>Total diagnostic messages per group per day<input name="budget" type="number" min="1" max="100000" required defaultValue={plan.max_messages_per_day} className="block w-full rounded border"/></label>
        <label>Start time (Moscow)<input name="start" type="time" required defaultValue={plan.hours_start} className="block rounded border"/></label>
        <label>End time (Moscow)<input name="end" type="time" required defaultValue={plan.hours_end} className="block rounded border"/></label>
      </div><fieldset><legend>Schedule days</legend><div className="flex gap-3 flex-wrap">{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((day,i)=><label key={day}><input name="day" type="checkbox" value={i} defaultChecked={plan.sending_days.includes(i)}/>{day}</label>)}</div></fieldset>
      <label className="flex gap-2 text-sm"><input name="autoReply" type="checkbox" defaultChecked={plan.auto_reply}/>Automatically send a labelled diagnostic reply after confirmed receipt</label>
      <button disabled={busy} className="rounded bg-blue-700 px-4 py-2 text-white">Save settings and pause group</button>
    </form>}
    <p className="text-sm">Group budget used today: {plan.used_today} / {plan.max_messages_per_day}</p><div className="flex flex-wrap gap-2 text-xs">{Object.entries(c).map(([status,count])=><span className="rounded border px-2 py-1" key={status}>{translate(status)}: {count}</span>)}{!Object.keys(c).length&&<span>No queued cycle yet.</span>}</div>
    <p className="text-xs text-gray-600">Stopped schedules retain their queue. Resuming continues pending checks. A new cycle is not added while the previous cycle has pending checks.</p>
    <div className="flex flex-wrap gap-2"><button disabled={busy} className="rounded border px-3 py-2 text-sm" onClick={()=>onAction(()=>api.post(`/local/diagnostics/plans/${plan.id}/enqueue`,{}))}>Queue next cycle</button><button disabled={busy} className="rounded border px-3 py-2 text-sm" onClick={()=>onAction(()=>api.post(`/local/diagnostics/plans/${plan.id}/cancel-queue`,{}))}>Cancel pending checks</button></div>
  </div>;
}
