import { useEffect,useState } from 'react';
import { api } from '../api';

export default function SendLog(){
  const [rows,setRows]=useState([]),[accounts,setAccounts]=useState([]),[inbox,setInbox]=useState(''),[error,setError]=useState('');
  useEffect(()=>{api.get('/inboxes').then(setAccounts).catch(e=>setError(e.message));},[]);
  useEffect(()=>{const load=()=>api.get('/local/operations?limit=500'+(inbox?`&inbox_id=${inbox}`:'')).then(setRows).catch(e=>setError(e.message));load();const timer=setInterval(load,15000);return()=>clearInterval(timer);},[inbox]);
  return <div className="flex-1 min-h-0 overflow-auto p-6 space-y-4"><h1 className="text-2xl font-semibold">Sending log</h1><p className="text-sm text-gray-600">Reserved or uncertain operations are never retried automatically. Check the mailbox before deciding to send a new message.</p><label>Sender<select className="ml-2 rounded border-gray-300" value={inbox} onChange={e=>setInbox(e.target.value)}><option value="">All accounts</option>{accounts.map(a=><option key={a.id} value={a.id}>{a.email}</option>)}</select></label>{error&&<p role="alert" className="text-red-700">{error}</p>}
    <div className="overflow-x-auto rounded border bg-white"><table className="w-full text-sm text-left"><thead><tr><th className="p-3">Date</th><th className="p-3">Sender</th><th className="p-3">Recipient</th><th className="p-3">Type</th><th className="p-3">Status</th><th className="p-3">Message-ID / error</th></tr></thead><tbody>{rows.map(r=><tr key={r.id} className="border-t"><td className="p-3">{new Date(r.created_at+'Z').toLocaleString()}</td><td className="p-3">{accounts.find(a=>a.id===r.inbox_id)?.email || r.inbox_id}</td><td className="p-3">{r.recipient}</td><td className="p-3">{r.kind}</td><td className="p-3">{r.status}</td><td className="p-3 break-all">{r.message_id || r.error || '—'}</td></tr>)}</tbody></table>{!rows.length&&<p className="p-8 text-center text-gray-500">No send attempts yet.</p>}</div>
  </div>;
}
