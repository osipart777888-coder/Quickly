import { useEffect, useState } from 'react';
import { api } from '../../api';

const input = 'block w-full rounded-md border border-gray-300 bg-transparent p-2 text-sm';
const button = 'rounded-md border border-gray-300 px-3 py-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed';
const labels = { yandex: 'Яндекс Карты', '2gis': '2ГИС', spark: 'СПАРК', rusprofile: 'Rusprofile' };
const states = { running: 'Собирает карточки', completed: 'Сбор завершён', limited: 'Достигнут лимит карточек', completed_with_errors: 'Завершён с ошибками', paused: 'На паузе', error: 'Ошибка', needs_auth: 'Нужна проверка доступа', interrupted: 'Прерван — можно продолжить', saved: 'Старая выгрузка' };
function emailStage(run) {
  if (run.status === 'running') return 'Сбор карточек компаний';
  const progress = run.email_progress;
  if (progress?.sites_pending) return 'Поиск email на сайтах';
  if (run.email_status === 'queued' || run.email_status === 'running') return 'Импорт контактов';
  if (run.email_status === 'completed_with_errors') return 'Поиск завершён, часть сайтов недоступна';
  if (run.email_status === 'completed') return 'Поиск email завершён';
  if (run.status === 'completed' && !run.saved) return 'Карточки прочитаны, ожидается импорт';
  return states[run.status] || run.status;
}
const split = text => text.split(/[;\n]+/).map(s => s.trim()).filter(Boolean);

export default function LocalCompanyParser({ onResults, historyOnly = false }) {
  const [state, setState] = useState({ connected: false, runs: [], message: 'Проверка соединения…' });
  const [form, setForm] = useState({ source: 'yandex', name: 'Сбор из карт', query: '', cities: '', rubrics: '', urls: '', limit: 100, timeout: 900, delay: 1, visible: false, crawl_websites: true, rights_confirmed: false, rights_basis: '' });
  const [busy, setBusy] = useState(false), [message, setMessage] = useState(''), [cities, setCities] = useState([]), [expanded, setExpanded] = useState('');
  const [savedImport, setSavedImport] = useState(null), [importBasis, setImportBasis] = useState('');
  const change = (key, value) => setForm(f => ({ ...f, [key]: value }));
  useEffect(() => {
    let cancelled = false;
    const load = () => api.get('/collector/local-parser').then(s => { if (!cancelled) setState(s); }).catch(e => { if (!cancelled) setMessage(e.message); });
    load(); const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (historyOnly || !state.connected || !['yandex', '2gis'].includes(form.source)) { setCities([]); return; }
    let cancelled = false;
    api.get('/collector/local-parser/catalog?source=' + form.source).then(data => { if (!cancelled) setCities(data); }).catch(e => { if (!cancelled) setMessage(e.message); });
    return () => { cancelled = true; };
  }, [state.connected, form.source, historyOnly]);
  async function run(action) {
    setBusy(true); setMessage('');
    try { await action(); setState(await api.get('/collector/local-parser')); }
    catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }
  async function start(event) {
    event.preventDefault();
    await run(async () => {
      await api.post('/collector/local-parser/runs', { ...form, cities: split(form.cities), rubrics: split(form.rubrics), urls: split(form.urls) });
      setMessage('Сбор запущен на Mac. После чтения карточек Quickly проверит сайты и покажет найденные email в результатах. Можно закрыть эту страницу.');
    });
  }
  async function importRun(id, confirmed = false) {
    await run(async () => {
      const result = await api.post('/collector/local-parser/runs/' + id + '/import', { rights_confirmed: confirmed, rights_basis: importBasis });
      setSavedImport(null); setImportBasis('');
      setMessage(result.already_imported ? 'Эта версия уже импортирована. Новые дубликаты не создавались.' : result.rows ? 'На проверку поставлено: ' + result.rows + ' карточек. В базу попадут только компании с найденным email.' : 'В этой выгрузке пока нет компаний.');
    });
  }
  const maps = ['yandex', '2gis'].includes(form.source);
  const active = state.runs.some(r => r.status === 'running');
  return <div className="space-y-5">
    {!historyOnly && <section className="rounded-xl border bg-white p-5 space-y-3">
      <div className="flex flex-wrap justify-between gap-3"><h2 className="text-lg font-semibold">Ваш локальный парсер</h2><button className={button} disabled={busy} onClick={() => run(async () => {})}>Обновить</button></div>
      <p className="text-sm">{state.connected ? 'Подключён к Chrome на этом Mac. Яндекс Карты и 2ГИС работают через существующий парсер без API-ключей.' : state.message}</p>
      <p className="text-sm text-gray-500">Для СПАРК и Rusprofile нужны доступ и выборка в отдельном браузере парсера. При ограничении сервиса сбор остановится; причина появится ниже. Письма не отправляются.</p>
      {!state.connected && <p className="text-sm">Подключение: в папке Quickly запустите <code>python3 scripts/setup_local_parser.py</code>, затем перезапустите Quickly.</p>}
    </section>}
    {message && <div role="status" className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm whitespace-pre-wrap">{message}</div>}
    {!historyOnly && <form onSubmit={start} className="rounded-xl border bg-white p-5 space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <label className="text-sm">Источник<select className={input} value={form.source} onChange={e => change('source', e.target.value)}>{Object.entries(labels).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
        <label className="text-sm">Название запуска<input className={input} value={form.name} onChange={e => change('name', e.target.value)}/></label>
        <label className="text-sm">Сколько компаний проверить<input type="number" min="1" max="10000" required className={input} value={form.limit} onChange={e => change('limit', Number(e.target.value))}/></label>
        {maps ? <>
          <label className="text-sm">Тематика / поисковая фраза<input className={input} required={form.source === 'yandex' || !form.rubrics.trim()} placeholder="Например: юридические компании" value={form.query} onChange={e => change('query', e.target.value)}/></label>
          <label className="text-sm md:col-span-2">Города через точку с запятой<input required className={input} placeholder="Тула; Рязань; Калуга" list="parser-cities" value={form.cities} onChange={e => change('cities', e.target.value)}/><datalist id="parser-cities">{cities.map(c => <option key={c.code} value={c.name}/>)}</datalist><span className="text-xs text-gray-500">Лимит — количество проверяемых карточек за весь запуск. В результаты попадут только компании с найденным email; их может быть меньше лимита.</span></label>
          {form.source === '2gis' && <label className="text-sm">Коды рубрик 2ГИС (необязательно)<input className={input} placeholder="Например: 344" value={form.rubrics} onChange={e => change('rubrics', e.target.value)}/></label>}
        </> : <div className="md:col-span-3 space-y-3"><button type="button" className={button} disabled={busy || active || !state.connected} onClick={() => run(async () => { await api.post('/collector/local-parser/access/' + form.source, {}); setMessage('Открывается отдельное окно Chrome для входа. Выберите нужные фильтры на сайте и вставьте ссылку выборки ниже. Пароли вводите только на сайте сервиса.'); })}>Открыть окно входа в {labels[form.source]}</button><label className="block text-sm">HTTPS-ссылки выборки / карточек, по одной на строке<textarea className={input} rows="3" placeholder="Пусто — использовать сохранённую в парсере выборку" value={form.urls} onChange={e => change('urls', e.target.value)}/></label>{state.access?.message && <p className="text-sm">{state.access.message}</p>}</div>}
      </div>
      <details className="text-sm"><summary className="cursor-pointer">Скорость и браузер</summary><div className="grid md:grid-cols-3 gap-4 mt-3">
        <label>Задержка между карточками, секунд<input type="number" min="0.5" max="60" step="0.5" className={input} value={form.delay} onChange={e => change('delay', Number(e.target.value))}/></label>
        <label>Тайм-аут одного задания, секунд<input type="number" min="30" max="3600" className={input} value={form.timeout} onChange={e => change('timeout', Number(e.target.value))}/></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.visible} onChange={e => change('visible', e.target.checked)}/>Показать Chrome при сборе</label>
      </div></details>
      <p className="text-sm text-gray-500">Ищем публичные email в карточках и на сайтах. Компании без найденной почты не добавляются в результаты. Телефоны сохраняются отдельным полем.</p>
      <label className="block text-sm">Основание обработки выбранных данных<input required maxLength="2000" className={input} placeholder="Укажите своё основание" value={form.rights_basis} onChange={e => change('rights_basis', e.target.value)}/></label>
      <label className="flex items-start gap-2 text-sm"><input required type="checkbox" checked={form.rights_confirmed} onChange={e => change('rights_confirmed', e.target.checked)}/>Подтверждаю права на хранение и обработку выбранных данных</label>
      <button className={button + ' bg-blue-700 text-white'} disabled={busy || !state.connected || !state.parser_available || active}>Начать поиск почт</button>
      {active && <span className="ml-3 text-sm text-gray-500">Один запуск уже выполняется. Для нового дождитесь его завершения или поставьте на паузу.</span>}
    </form>}
    <section className="rounded-xl border bg-white p-5 space-y-4">
      <h2 className="text-lg font-semibold">Запуски и сохранённые выгрузки</h2>
      <p className="text-sm text-gray-500">Новые завершённые и приостановленные запуски импортируются автоматически. Старые выгрузки можно добавить кнопкой «Импортировать». Повторный импорт той же версии не создаёт новых записей.</p>
      {!state.runs.length && <p className="text-sm">Сохранённых запусков пока нет.</p>}
      {state.runs.map(r => <article key={r.id} className="rounded-lg border p-4 space-y-2">
        <div className="flex justify-between gap-3"><strong className="break-all">{r.name}</strong><span className="text-sm whitespace-nowrap">{emailStage(r)}</span></div>
        <p className="text-sm">{labels[r.source] || r.source} · Проверено карточек: {r.stats.companies} · Уникальных email: {r.email_progress?.unique_emails ?? r.stats.emails}{r.email_progress ? ` · Компаний с почтой: ${r.email_progress.with_email}` : ''}</p>
        {r.email_progress && <p className="text-sm text-gray-500">Проверено сайтов: {r.email_progress.sites_checked} · В очереди: {r.email_progress.sites_pending} · Недоступно: {r.email_progress.sites_failed}</p>}
        {r.coverage?.planned > 0 && <div className="text-sm space-y-1"><p>{r.coverage.country} · Посещено населённых пунктов: {r.coverage.visited || 0} / {r.coverage.planned} · Полностью проверено: {r.coverage.completed || 0}</p><p className="text-xs text-gray-500">{r.coverage.note}</p></div>}
        {r.message && <p className="text-sm text-red-700 whitespace-pre-wrap">{r.message}</p>}
        <div className="flex flex-wrap gap-2">
          {!r.saved && r.status !== 'limited' && <button disabled={busy} className={button} onClick={() => run(() => api.post('/collector/local-parser/runs/' + r.id + '/' + (r.status === 'running' ? 'pause' : 'resume'), {}))}>{r.status === 'running' ? 'Пауза' : 'Продолжить'}</button>}
          {r.status !== 'running' && <button disabled={busy} className={button} onClick={() => r.saved ? setSavedImport(r.id) : importRun(r.id)}>Импортировать в Quickly</button>}
          {r.job_id && <button className={button} onClick={() => onResults(r.job_id, r.source)}>Найденные почты</button>}
          <button className={button} onClick={() => setExpanded(expanded === r.id ? '' : r.id)}>Подробности</button>
        </div>
        {expanded === r.id && <div className="text-sm space-y-1">{r.tasks.map((t, i) => <p key={i}>{t.city} · {t.query}: {t.status}, карточек {t.count}{t.message ? ' — ' + t.message : ''}</p>)}</div>}
      </article>)}
    </section>
    {savedImport && <div role="dialog" aria-label="Импорт старой выгрузки" className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50"><form className="bg-white rounded-xl p-6 max-w-xl w-full space-y-4" onSubmit={e => { e.preventDefault(); importRun(savedImport, true); }}><h2 className="font-semibold">Импорт сохранённой выгрузки</h2><p className="text-sm">Исходная база парсера сохранится. Контакты появятся в базе Quickly с указанием источников.</p><label className="block text-sm">Основание хранения и обработки<input required className={input} maxLength="2000" value={importBasis} onChange={e => setImportBasis(e.target.value)}/></label><label className="flex gap-2 text-sm"><input type="checkbox" required/>Подтверждаю права на обработку этой выгрузки</label><div className="flex gap-2"><button className={button} disabled={busy}>Импортировать</button><button type="button" className={button} onClick={() => setSavedImport(null)}>Отмена</button></div></form></div>}
  </div>;
}
