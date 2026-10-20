import { useEffect,useState } from 'react';
import { Link } from 'react-router-dom';
import { ResponsiveContainer,LineChart,Line,XAxis,YAxis,Tooltip,Legend } from 'recharts';
import { api } from '../api';
import AccountOverview from '../components/local/AccountOverview';

export default function LocalDashboard(){
  const [data,setData]=useState(null),[error,setError]=useState('');
  useEffect(()=>{let live=true;const load=async()=>{try{const d=await api.get('/local/overview');if(live)setData(d);}catch(e){if(live)setError(e.message);}};load();const timer=setInterval(load,30000);return()=>{live=false;clearInterval(timer);};},[]);
  if(!data)return <div className="p-8">{error||'Loading…'}</div>;
  const cards=[['Connected accounts',data.accounts.filter(x=>x.connected).length],['Active accounts',data.accounts.filter(x=>x.connected&&!x.paused).length],['Accounts with errors',data.accounts.filter(x=>x.last_error).length],['Accepted today',data.today],['Accepted this month',data.month],['Delivery errors today',data.errors_today],['Replies',data.replies],['Unsubscribed',data.unsubscribed],['Uncertain results',data.uncertain_today]];
  return <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-6"><div><h1 className="text-2xl font-semibold">Home</h1><p className="text-sm text-gray-500">Your local Quickly installation. No paid AI API is required.</p></div>
    <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">{cards.map(([label,value])=><div key={label} className="rounded-xl border bg-white p-4"><p className="text-sm text-gray-500">{label}</p><p className="mt-2 text-3xl font-semibold">{value}</p></div>)}</div>
    <p className="rounded border border-blue-100 bg-blue-50 p-3 text-sm">Accepted by the sending server does not mean delivered to the recipient. Confirmed receipt is shown only when diagnostics or a provider report supplies evidence.</p>
    <section className="rounded-xl border bg-white p-4"><h2 className="font-semibold mb-3">Sending activity — 30 days</h2><div className="h-64"><ResponsiveContainer><LineChart data={data.timeline}><XAxis dataKey="date" tickFormatter={v=>v.slice(5)}/><YAxis allowDecimals={false}/><Tooltip/><Legend/><Line type="monotone" dataKey="accepted" name="Accepted" stroke="#0d9488"/><Line type="monotone" dataKey="diagnostic" name="Diagnostics" stroke="#2563eb"/><Line type="monotone" dataKey="errors" name="Errors" stroke="#dc2626"/></LineChart></ResponsiveContainer></div></section>
    <AccountOverview compact/>
    <section className="rounded-xl border bg-white p-4"><h2 className="font-semibold">Recent diagnostics</h2>{data.diagnostics.map(run=><p key={run.id} className="py-2 text-sm">{new Date(run.created_at+'Z').toLocaleString()} · {run.status} · {run.folder||'—'}</p>)}{!data.diagnostics.length&&<p className="py-4 text-gray-500">No diagnostics yet. Connect two of your own mailboxes to run a test.</p>}<Link to="/diagnostics" className="text-blue-700">Open diagnostics</Link></section>
  </div>;
}
