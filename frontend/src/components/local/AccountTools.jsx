import { useState } from 'react';
import { api } from '../../api';
import AccountOverview from './AccountOverview';

export default function AccountTools({onChanged}) {
  const [preset,setPreset]=useState('yandex'),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[show,setShow]=useState(false);
  const hosts=preset==='yandex'?['smtp.yandex.ru','imap.yandex.ru']:preset==='mailru'?['smtp.mail.ru','imap.mail.ru']:['',''];
  async function add(e){e.preventDefault();setBusy(true);setMessage('');const f=Object.fromEntries(new FormData(e.currentTarget));try{
    await api.post('/local/accounts',{email:f.email,display_name:f.display_name,max_emails_per_day:f.limit,smtp_host:f.smtp_host,smtp_port:f.smtp_port,smtp_username:f.email,smtp_password:f.password,imap_host:f.imap_host,imap_port:f.imap_port,imap_username:f.email,imap_password:f.password});
    setMessage('Account saved. Use Test connection to check SMTP and IMAP.');e.target.reset();onChanged?.();
  }catch(err){setMessage(err.message);}finally{setBusy(false);}}
  async function upload(e){const file=e.target.files?.[0];e.target.value='';if(!file)return;setBusy(true);try{const r=await api.upload('/local/accounts/import',file);setMessage(`Сохранено: ${r.saved}; ошибок: ${r.invalid}. `+r.errors.map(x=>`Строка ${x.row}: ${x.error}`).join('; '));onChanged?.();}catch(err){setMessage(err.message);}finally{setBusy(false);}}
  function template(){const text='email,max_emails_per_day,smtp_host,smtp_port,smtp_username,smtp_password,imap_host,imap_port,imap_username,imap_password\nowner@example.ru,5,smtp.example.ru,465,owner@example.ru,APP_PASSWORD,imap.example.ru,993,owner@example.ru,APP_PASSWORD\n';const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='quickly-accounts-template.csv';a.click();URL.revokeObjectURL(url);}
  async function trust(e){e.preventDefault();setBusy(true);try{const hostname=new FormData(e.currentTarget).get('hostname');await api.post('/local/trusted-mail-hosts',{hostname});setMessage('Точный почтовый сервер добавлен в список доверенных для VPN. Повторите проверку подключения.');}catch(err){setMessage(err.message);}finally{setBusy(false);}}
  return <div className="space-y-4"><AccountOverview/>
    <section className="rounded-xl border bg-white p-4 space-y-3"><div className="flex flex-wrap gap-3 items-center"><button className="rounded bg-blue-700 px-3 py-2 text-white" onClick={()=>setShow(!show)}>Add Yandex / Mail.ru / corporate mailbox</button><label className="cursor-pointer rounded border px-3 py-2">Import account CSV<input type="file" accept=".csv" className="hidden" disabled={busy} onChange={upload}/></label><button onClick={template} className="text-blue-700">Download account CSV template</button></div>
    <p className="text-sm text-gray-600">For Gmail and Outlook, click Add Inbox and select Google or Microsoft OAuth. Use an app password for Yandex and Mail.ru. Imported passwords are encrypted in the database; keep the original CSV in a safe place.</p>
    {show&&<form onSubmit={add} className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-4xl" key={preset}>
      <label>Provider<select value={preset} onChange={e=>setPreset(e.target.value)} className="block w-full rounded border-gray-300"><option value="yandex">Yandex</option><option value="mailru">Mail.ru</option><option value="custom">Corporate / other SMTP</option></select></label>
      <label>Email<input name="email" type="email" required className="block w-full rounded border-gray-300"/></label><label>Sender name<input name="display_name" className="block w-full rounded border-gray-300"/></label><label>Daily limit<input name="limit" type="number" defaultValue="5" min="1" max="100000" required className="block w-full rounded border-gray-300"/></label>
      <label>SMTP server<input name="smtp_host" defaultValue={hosts[0]} required className="block w-full rounded border-gray-300"/></label><label>SMTP port<input name="smtp_port" type="number" defaultValue="465" min="1" max="65535" required className="block w-full rounded border-gray-300"/></label>
      <label>IMAP server<input name="imap_host" defaultValue={hosts[1]} className="block w-full rounded border-gray-300"/></label><label>IMAP port<input name="imap_port" type="number" defaultValue="993" className="block w-full rounded border-gray-300"/></label>
      <label>App password<input name="password" type="password" autoComplete="new-password" required className="block w-full rounded border-gray-300"/></label><div className="flex items-end"><button disabled={busy} className="rounded bg-blue-700 px-4 py-2 text-white">Save mailbox</button></div>
    </form>}
    <details className="text-sm"><summary className="cursor-pointer">VPN: разрешить точное имя корпоративного почтового сервера</summary><p className="my-2 text-gray-600">Если VPN выдаёт синтетический адрес DNS, добавьте точное имя своего SMTP или IMAP-сервера. Частные сети и localhost остаются заблокированы; сертификаты TLS проверяются.</p><form onSubmit={trust} className="flex gap-2 max-w-xl"><input name="hostname" placeholder="smtp.your-company.ru" required className="rounded border"/><button disabled={busy} className="rounded border px-3">Добавить сервер</button></form></details>
    {message&&<p role="status" className="text-sm whitespace-pre-wrap">{message}</p>}
    </section></div>;
}
