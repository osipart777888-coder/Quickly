import { useState } from 'react';
import { api } from '../../api';

export default function DailyLimitSettings({ inboxes, onSaved }) {
  const [selectedId, setSelectedId] = useState('');
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const selected = inboxes.find(inbox => String(inbox.id) === selectedId) || inboxes[0];
  if (!selected) return null;

  const all = selectedId === 'all';
  const id = all ? 'all' : String(selected.id);
  const commonLimit = inboxes.every(inbox => inbox.max_emails_per_day === selected.max_emails_per_day) ? String(selected.max_emails_per_day) : '';
  const value = draft?.id === id ? draft.value : all ? commonLimit : String(selected.max_emails_per_day);
  const limit = Number(value);
  const valid = value !== '' && Number.isInteger(limit) && limit >= 1 && limit <= 100000;
  const changed = all ? inboxes.some(inbox => inbox.max_emails_per_day !== limit) : selected.max_emails_per_day !== limit;

  async function save(event) {
    event.preventDefault();
    if (busy || !valid || !changed) return;
    setBusy(true);
    setMessage(null);
    try {
      const saved = all
        ? await api.post('/inboxes/daily-limit', { inbox_ids: inboxes.map(inbox => inbox.id), max_emails_per_day: limit })
        : await api.patch(`/inboxes/${selected.id}`, { max_emails_per_day: limit });
      setDraft({ id, value: String(saved.max_emails_per_day) });
      await onSaved?.(saved);
      setMessage({ error: false, text: all
        ? `Для ${saved.updated} ящиков сохранён лимит: ${saved.max_emails_per_day} в сутки на каждый ящик.`
        : `Лимит для ${selected.email} сохранён: ${saved.max_emails_per_day} в сутки.` });
    } catch (error) {
      setMessage({ error: true, text: error.message || 'Не удалось сохранить лимит. Повторите попытку.' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Дневной лимит отправки" className="mb-6 rounded-xl border bg-white p-4 space-y-3">
      <h2 className="font-semibold">Дневной лимит отправки</h2>
      <form onSubmit={save} className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-0 basis-64 text-sm">
          <label htmlFor="daily-limit-account">Почтовый ящик</label>
          <select id="daily-limit-account" value={id} disabled={busy} onChange={event => { setSelectedId(event.target.value); setDraft(null); setMessage(null); }} className="mt-1 block w-full rounded border-gray-300">
            <option value="all">Все ящики ({inboxes.length})</option>
            {inboxes.map(inbox => <option key={inbox.id} value={String(inbox.id)}>{inbox.email}{inbox.display_name ? ` — ${inbox.display_name}` : ''}</option>)}
          </select>
        </div>
        <div className="w-56 max-w-full text-sm">
          <label htmlFor="daily-limit-value">Максимум писем в сутки</label>
          <input id="daily-limit-value" type="number" min="1" max="100000" step="1" required value={value} placeholder={all ? 'Лимит на каждый ящик' : undefined} disabled={busy} onChange={event => { setDraft({ id, value: event.target.value }); setMessage(null); }} className="mt-1 block w-full rounded border-gray-300" />
        </div>
        <button type="submit" disabled={busy || !valid || !changed} className="rounded bg-blue-700 px-4 py-2 text-white disabled:opacity-50 disabled:cursor-not-allowed">{busy ? 'Сохранение…' : all ? 'Сохранить для всех' : 'Сохранить лимит'}</button>
      </form>
      {all && <p className="text-sm font-medium">Значение будет установлено для каждого из {inboxes.length} ящиков по отдельности.</p>}
      <p className="text-xs text-gray-600">Лимит задаётся отдельно для выбранного ящика и включает кампании, тесты и диагностику. При постепенном увеличении лимита сегодня может действовать меньшее значение.</p>
      {message && <p role={message.error ? 'alert' : 'status'} className={`text-sm ${message.error ? 'text-red-700' : 'text-green-700'}`}>{message.text}</p>}
    </section>
  );
}
