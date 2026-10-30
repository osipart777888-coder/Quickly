import { useEffect, useState } from 'react';
import { api } from '../../api';
import { Link } from 'react-router-dom';

export default function AccountOverview({compact=false, refreshRevision=0}) {
  const [data,setData]=useState(null), [error,setError]=useState(''), [busy,setBusy]=useState(null);
  async function load(){try{setData(await api.get('/local/overview'));}catch(e){setError(e.message);}}
  useEffect(()=>{load();const timer=setInterval(load,30000);return()=>clearInterval(timer);},[refreshRevision]);
  async function test(id, provider){setBusy(id);setError('');try{const report=await api.post(provider==='smtp'?`/smtp/inboxes/${id}/test`:`/local/accounts/${id}/test`,{}); if(!report.ok)setError([report.smtp?.error,report.imap?.error].filter(Boolean).join('; '));await load();}catch(e){setError(e.message);}finally{setBusy(null);}}
  if(!data)return <p>{error || 'Loading account status…'}</p>;
  return <section className="rounded-xl border bg-white p-4 space-y-3">
    <div className="flex flex-wrap justify-between gap-2"><h2 className="font-semibold">Account limits and connection status</h2><Link to="/diagnostics" className="text-blue-700">Warm-up & diagnostics</Link></div>
    <p className="text-xs text-gray-500">Daily attempts include campaigns, tests and diagnostics. Server acceptance and confirmed receipt are counted separately.</p>
    {error&&<p role="alert" className="text-red-700 text-sm">{error}</p>}
    <div className="overflow-x-auto"><table className="w-full min-w-max text-sm text-left"><thead><tr className="border-b text-gray-500">
      <th className="p-2">Email</th><th className="p-2">Provider</th><th className="p-2">Status</th><th className="p-2">Daily limit</th><th className="p-2">Accepted today</th><th className="p-2">Attempts today</th><th className="p-2">SMTP</th><th className="p-2">IMAP</th><th className="p-2">Replies</th><th className="p-2">Last success</th><th className="p-2">Last error</th><th className="p-2">Actions</th>
    </tr></thead><tbody>{data.accounts.map(a=><tr key={a.id} className="border-b">
      <td className="p-2 font-mono text-xs">{a.email}</td><td className="p-2">{a.smtp_host.includes('yandex')?'Yandex':a.smtp_host.includes('mail.ru')?'Mail.ru':a.provider==='gmail'?'Google OAuth':a.provider==='office365'?'Microsoft OAuth':'SMTP / IMAP'}</td>
      <td className="p-2">{a.paused?'Paused':a.connected?'Configured':'Not connected'}</td><td className="p-2">{a.effective_limit} / {a.max_emails_per_day}{a.ramp_up_enabled&&<span className="block text-xs">Ramp-up enabled</span>}</td>
      <td className="p-2">{a.sent_today}</td><td className="p-2">{a.attempts_today}</td><td className="p-2">{a.smtp_status}</td><td className="p-2">{a.imap_status}</td><td className="p-2">{a.reply_count}</td>
      <td className="p-2">{a.last_success_at?new Date(a.last_success_at+'Z').toLocaleString():'—'}</td><td className="p-2 max-w-xs whitespace-normal text-red-700">{a.last_error || '—'}</td>
      <td className="p-2"><button disabled={busy!==null} className="rounded border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-60" onClick={()=>test(a.id,a.provider)}>{busy===a.id?'Checking…':'Test connection'}</button></td>
    </tr>)}</tbody></table>{!data.accounts.length&&<p className="py-6 text-center text-gray-500">No sending accounts yet. Add your own mailboxes below.</p>}</div>
    {!compact&&<p className="text-xs text-gray-500">A connection test checks credentials and transport without sending a message. Configure each daily limit within your provider's rules.</p>}
  </section>;
}
