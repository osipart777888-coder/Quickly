import {useState} from 'react';
import {api} from '../../api';
export default function CampaignReadiness({id,refresh}) {
  const [report,setReport]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function check(){setBusy(true);try{setReport(await api.get(`/local/campaigns/${id}/preflight`));setError('');}catch(e){setError(e.message);}finally{setBusy(false);}}
  async function cancel(){if(!window.confirm('Отменить кампанию и удалить ожидающую очередь? История будет сохранена. Для нового запуска понадобится копия кампании.'))return;setBusy(true);try{const r=await api.post(`/local/campaigns/${id}/cancel`,{});setError(r.message);await refresh();await check();}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className="mb-5 rounded-lg border bg-white p-3 space-y-2"><div className="flex flex-wrap gap-3"><button disabled={busy} onClick={check} className="rounded border px-3 py-1.5">Check campaign readiness</button><button disabled={busy} onClick={cancel} className="rounded border border-red-200 px-3 py-1.5 text-red-700">Cancel campaign and queue</button></div>{error&&<p role="status" className="text-sm">{error}</p>}{report&&<div className="text-sm"><p>{report.ready?'Campaign configuration is ready. Global sending permission and campaign schedule still apply.':'Campaign needs attention:'}</p><ul className="list-disc pl-5">{report.errors.map((s,i)=><li key={i}>{s}</li>)}</ul></div>}</section>;
}
