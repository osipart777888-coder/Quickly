import { useEffect, useState } from 'react';
import { api } from '../../api';
import { language, switchLanguage } from '../../i18n';

export default function ControlBar() {
  const [control, setControl] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  async function load() { try { setControl(await api.get('/local/control')); } catch(e) { setError(e.message); } }
  useEffect(() => { load(); const timer=setInterval(load, 15000); return () => clearInterval(timer); }, []);
  async function update(values) {
    try { setControl(await api.put('/local/control', values)); setError(''); setExpanded(false); setConfirmation(''); }
    catch(e) { setError(e.message); }
  }
  return <div className="shrink-0 border-b border-gray-200 bg-white px-4 py-2 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className={control?.emergency_stopped ? 'font-semibold text-red-700' : 'text-gray-600'}>
        {control?.emergency_stopped ? 'Emergency stop is active' : control?.campaigns_enabled ? 'Campaign sending is enabled' : 'Campaign sending is locked'}
      </span>
      <div className="flex flex-wrap gap-2">
        <button onClick={switchLanguage} title="Save changes before switching language" className="rounded border px-3 py-1 whitespace-nowrap">{language.toUpperCase()} / {language === 'ru' ? 'EN' : 'RU'}</button>
        {control?.emergency_stopped ? <button className="rounded border px-3 py-1 whitespace-nowrap" onClick={() => update({emergency_stopped:false})}>Release emergency stop</button> :
          <button className="rounded bg-red-700 px-3 py-1 text-white" onClick={() => update({emergency_stopped:true})}>Stop all sending</button>}
        {!control?.emergency_stopped && <button className="rounded border px-3 py-1 whitespace-nowrap" onClick={() => control?.campaigns_enabled ? update({campaigns_enabled:false}) : setExpanded(!expanded)}>{control?.campaigns_enabled ? 'Lock campaigns' : 'Enable campaigns…'}</button>}
      </div>
    </div>
    {expanded && <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 space-y-2">
      <p>Only enable campaigns for recipients you are permitted to contact. Every contact needs a permission date and basis. Public unsubscribe links require a separate HTTPS address.</p>
      <label className="block">Type РАЗРЕШАЮ ОТПРАВКУ to enable real campaigns<input value={confirmation} onChange={e=>setConfirmation(e.target.value)} className="block mt-1 rounded border-gray-300" /></label>
      <button className="rounded bg-blue-700 px-3 py-1 text-white" onClick={()=>update({campaigns_enabled:true,confirmation})}>Confirm real sending</button>
    </div>}
    {error && <p role="alert" className="mt-2 text-red-700">{error}</p>}
  </div>;
}
