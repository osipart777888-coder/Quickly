import { useEffect, useState } from 'react';
import { api } from '../api';
import { Link } from 'react-router-dom';
import LocalCompanyParser from '../components/local/LocalCompanyParser';
import CompanyCollectionForm from '../components/local/CompanyCollectionForm';
import { collectionSources, resultSource, collectionRequest } from '../components/local/companyCollectionRequest';

const initial = { name: 'Сбор компаний', sources: ['website'], scope: 'cities', country_code: 'RU', country: 'Россия', regions: [], cities: [], okved: [], query: '', employees_min: null, employees_max: null, limit: 100, only_active: false, only_email: true, exclude_existing: true, crawl_websites: true, check_mx: false, websites: [], rights_confirmed: false, rights_basis: '' };
const states = { ready: 'Доступен', blocked: 'Требует настройки', not_connected: 'Не подключён', queued: 'В очереди', running: 'Выполняется', paused: 'На паузе', stopped: 'Остановлен', completed: 'Завершён', completed_with_errors: 'Завершён с ошибками' };
const inputClass = 'block w-full rounded-md border border-gray-300 bg-transparent p-2 text-sm';
const buttonClass = 'rounded-md border border-gray-300 px-3 py-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed';
const panelClass = 'rounded-xl border border-gray-200 bg-white p-5 space-y-4';
function Field({ title, children }) { return <label className="block text-sm font-medium space-y-1"><span>{title}</span>{children}</label>; }
function Check({ title, checked, onChange, disabled }) { return <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} disabled={disabled} className="mt-1 rounded"/><span>{title}</span></label>; }
function date(value) { return value ? new Date(value.endsWith('Z') ? value : value + 'Z').toLocaleString('ru-RU') : '—'; }
function split(value) { return value.split(/[;,\n]+/).map(v => v.trim()).filter(Boolean); }

export default function CompanyCollector() {
  const [tab, setTab] = useState('collect'), [params, setParams] = useState(initial), [sources, setSources] = useState([]);
  const [collectionSource, setCollectionSource] = useState('yandex'), [collectionMethod, setCollectionMethod] = useState('browser');
  const [parserOptions, setParserOptions] = useState({ rubrics: '', timeout: 900, delay: 1, visible: false }), [sourceUrls, setSourceUrls] = useState({});
  const [jobs, setJobs] = useState([]), [stats, setStats] = useState({}), [table, setTable] = useState({ items: [], total: 0 });
  const [filters, setFilters] = useState({ q: '', city: '', region: '', okved: '', source: 'yandex', status: '', has_email: 'true', consent: '', job_id: '', only_active: false, employees_min: '', employees_max: '', exclude_existing: false, sort: 'created_at', descending: true });
  const [page, setPage] = useState(1), [selected, setSelected] = useState([]), [detail, setDetail] = useState(null);
  const [allSelected, setAllSelected] = useState(false), [excluded, setExcluded] = useState([]);
  const [file, setFile] = useState(null), [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  const [campaignId, setCampaignId] = useState(''), [campaigns, setCampaigns] = useState([]);
  const [consent, setConsent] = useState(null), [permissions, setPermissions] = useState([]);
  const [sourceEdit, setSourceEdit] = useState(null), [errors, setErrors] = useState(null);
  const [report, setReport] = useState({ enabled: false, recipient: '', inbox_id: null, attach_csv: false }), [accounts, setAccounts] = useState([]);
  const query = new URLSearchParams(Object.entries({ ...filters, has_email: true, page }).filter(([, v]) => v !== '' && v !== null && v !== undefined)).toString();
  const selectionCount = allSelected ? Math.max(0, table.total - excluded.length) : selected.length;
  function clearSelection() { setSelected([]); setAllSelected(false); setExcluded([]); }
  function toggleSelected(id, checked) {
    if (allSelected) setExcluded(ids => checked ? ids.filter(x => x !== id) : [...new Set([...ids, id])]);
    else setSelected(ids => checked ? [...new Set([...ids, id])] : ids.filter(x => x !== id));
  }
  function selectPage() {
    const ids = table.items.map(c => c.id);
    if (allSelected) setExcluded(values => values.filter(id => !ids.includes(id)));
    else setSelected(values => [...new Set([...values, ...ids])]);
  }
  function selectionPayload() {
    if (!allSelected) return { company_ids: selected };
    const { has_email, sort, descending, ...criteria } = filters;
    return { all_matching: true, excluded_ids: excluded,
      filters: Object.fromEntries(Object.entries(criteria).filter(([, value]) => value !== '' && value !== null && value !== undefined)) };
  }
  async function reload() {
    const [s, j, st] = await Promise.all([api.get('/collector/sources'), api.get('/collector/jobs'), api.get('/collector/stats')]);
    setSources(s); setJobs(j.items); setStats(st);
  }
  async function loadTable() { setTable(await api.get('/collector/companies?' + query)); }
  async function run(action) { setBusy(true); setMessage(''); try { return await action(); } catch (error) { setMessage(error.message); } finally { setBusy(false); } }
  useEffect(() => {
    reload().catch(e => setMessage(e.message));
    api.get('/collector/settings').then(s => { if (s.last_params && !s.last_params.parser_run) setParams({ ...initial, ...s.last_params, only_email: true, rights_confirmed: false }); if (s.report) setReport(s.report); }).catch(e => setMessage(e.message));
    api.get('/campaigns').then(c => setCampaigns(Array.isArray(c) ? c : c.items || [])).catch(() => {});
    api.get('/smtp/accounts').then(setAccounts).catch(() => {});
    const timer = setInterval(() => reload().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (tab !== 'results') return;
    let cancelled = false;
    const load = () => api.get('/collector/companies?' + query).then(data => { if (!cancelled) setTable(data); }).catch(e => { if (!cancelled) setMessage(e.message); });
    load(); const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [query, tab]);
  useEffect(() => { clearSelection(); setPage(1); }, [JSON.stringify(filters)]);
  useEffect(() => { if (tab === 'consents') api.get('/collector/consents?status=refused').then(setPermissions).catch(e => setMessage(e.message)); }, [tab, busy]);
  const change = (key, value) => setParams(p => ({ ...p, [key]: value }));
  const filter = (key, value) => setFilters(f => ({ ...f, [key]: value }));
  function chooseSource(source) {
    setCollectionSource(source); setCollectionMethod('browser'); setFile(null);
    setFilters(values => ({ ...values, source, job_id: '' }));
  }
  function changeParserOption(key, value) {
    if (key === 'urls') setSourceUrls(values => ({ ...values, [collectionSource]: value }));
    else setParserOptions(values => ({ ...values, [key]: value }));
  }
  function showParserResults(id, source) {
    setFilters(values => ({ ...values, q: '', city: '', region: '', okved: '', status: '', consent: '',
      source: resultSource(source), job_id: id, only_active: false, employees_min: '', employees_max: '', exclude_existing: false }));
    setTab('results');
  }
  function repeatJob(job) {
    const source = resultSource(job.params.sources?.[0]) || 'website';
    chooseSource(source);
    setCollectionMethod(job.params.parser_run ? 'browser' : 'api');
    setParams({ ...initial, ...job.params, only_email: true, rights_confirmed: false });
    setSourceUrls(values => ({ ...values, [source]: (job.params.urls || []).join('\n') }));
    setParserOptions({ rubrics: (job.params.rubrics || []).join('; '), timeout: job.params.timeout || 900,
      delay: job.params.delay || 1, visible: Boolean(job.params.visible) });
    setTab('collect');
  }
  async function start(event) {
    event.preventDefault();
    await run(async () => {
      const definition = collectionSources.find(source => source.key === collectionSource);
      const request = collectionRequest(collectionSource, collectionMethod, params, { ...parserOptions, urls: sourceUrls[collectionSource] || '' });
      if (request.path === '/collector/local-parser/runs') {
        await api.post(request.path, request.body);
        setMessage('Поиск запущен: ' + definition.name + '. Прогресс доступен в «Истории запусков», найденные email — в результатах этого источника.');
        setFilters(values => ({ ...values, source: collectionSource, job_id: '', city: '', region: '', q: '', okved: '',
          only_active: false, exclude_existing: false, employees_min: '', employees_max: '' }));
        setTab('history'); await reload(); return;
      }
      const job = request.upload ? await api.uploadMultipart(request.path, file, { params: JSON.stringify(request.body) }) : await api.post(request.path, request.body);
      setMessage('Запуск сохранён в очереди. Прогресс сохраняется после каждого пакета.');
      setFilters(f => ({ ...f, source: collectionSource, job_id: job.id, has_email: 'true', only_active: params.only_active, okved: params.okved.join(';'), exclude_existing: params.exclude_existing, employees_min: params.employees_min ?? '', employees_max: params.employees_max ?? '' }));
      setTab('history'); await reload();
    });
  }
  async function download(format) {
    await run(async () => {
      const response = await api.downloadPost('/collector/export/' + format, selectionPayload());
      const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = 'quickly-companies.' + format; link.click(); URL.revokeObjectURL(url);
      setMessage('Экспорт готов. Записей без email или разрешения на экспорт пропущено: ' + (response.headers.get('X-Collector-Omitted') || 0));
    });
  }
  function seeResults(job) { setFilters(f => ({ ...f, source: resultSource(job.params.sources?.length === 1 ? job.params.sources[0] : ''), job_id: job.id, has_email: 'true', only_active: job.params.only_active, okved: (job.params.okved || []).join(';'), exclude_existing: job.params.exclude_existing, employees_min: job.params.employees_min ?? '', employees_max: job.params.employees_max ?? '' })); setTab('results'); }
  async function openCompany(id) { await run(async () => setDetail(await api.get('/collector/companies/' + id))); }
  function consentForm(email) { setConsent({ email, evidence: '' }); }
  return <main aria-label="Сбор компаний" className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 lg:p-6">
    <div className="mx-auto w-full min-w-0 max-w-[1600px] space-y-5">
    <div><h1 className="text-2xl font-bold">Сбор компаний</h1><p className="text-sm text-gray-500 mt-1">Поиск организаций, публичные контакты и происхождение данных в единой базе Quickly.</p></div>
    <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2">{[['Компании с email', 'with_email'], ['Уникальные email', 'unique_emails'], ['Телефоны', 'phones'], ['Без контактов', 'without_contacts'], ['Без email', 'without_email'], ['Проверено сайтов', 'sites_checked'], ['Дубли', 'duplicates'], ['Ошибки', 'errors']].map(([label, key]) => <div key={key} className="rounded-lg border bg-white p-3"><div className="text-xs text-gray-500">{label}</div><strong className="text-xl">{stats[key] ?? 0}</strong></div>)}</div>
    <nav aria-label="Разделы сбора компаний" className="flex flex-wrap gap-2">{[['collect', 'Новый сбор'], ['results', 'Результаты'], ['history', 'История запусков'], ['sources', 'Источники и API'], ['consents', 'Отказы'], ['report', 'Отчёты'], ['help', 'Как пользоваться']].map(([id, title]) => <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)} className={`${buttonClass} ${tab === id ? 'bg-blue-700 text-white border-blue-700' : 'bg-white'}`}>{title}</button>)}</nav>
    {message && <div role="status" className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm whitespace-pre-wrap">{message}</div>}
    {tab === 'collect' && <CompanyCollectionForm params={params} change={change} source={collectionSource} onSource={chooseSource}
      method={collectionMethod} onMethod={setCollectionMethod} options={{ ...parserOptions, urls: sourceUrls[collectionSource] || '' }}
      onOptions={changeParserOption} sources={sources} file={file} onFile={setFile} busy={busy} onSubmit={start}
      onAccess={source => run(async () => { await api.post('/collector/local-parser/access/' + source, {}); setMessage('Открывается окно входа. Задайте фильтры в сервисе и вставьте ссылку на выборку.'); })}
      onSettings={() => setTab('sources')}/>}
    {tab === 'history' && <div className="space-y-5"><LocalCompanyParser historyOnly onResults={showParserResults}/><section className={panelClass}><h2 className="font-semibold">История запусков</h2>{!jobs.length && <p className="text-gray-500">Запусков пока нет.</p>}{jobs.map(job => <article className="border rounded-lg p-4 space-y-2" key={job.id}>
      <div className="flex flex-wrap justify-between gap-2"><strong>{job.name}</strong><span>{states[job.status] || job.status} · {date(job.created_at)}</span></div>
      <p className="text-sm">Компаний: {job.stats.companies}; с email: {job.stats.with_email}; без email: {job.stats.without_email}; сайтов: {job.sites_checked}; дублей: {job.duplicates}; ошибок: {job.errors}. Пакетов обработано: {job.tasks.done || 0}; осталось: {(job.tasks.pending || 0) + (job.tasks.retry || 0) + (job.tasks.running || 0)}.</p>
      {job.last_error && <p className="text-sm text-red-700">{job.last_error}</p>}
      <div className="flex flex-wrap gap-2"><button className={buttonClass} onClick={() => seeResults(job)}>Просмотреть результаты</button>
        {['queued', 'running'].includes(job.status) && <button disabled={busy} className={buttonClass} onClick={() => run(async () => { await api.post(`/collector/jobs/${job.id}/pause`, {}); await reload(); })}>Пауза</button>}
        {job.status === 'paused' && <button disabled={busy} className={buttonClass} onClick={() => run(async () => { await api.post(`/collector/jobs/${job.id}/resume`, {}); await reload(); })}>Продолжить</button>}
        {['queued', 'running', 'paused'].includes(job.status) && <button disabled={busy} className={buttonClass} onClick={() => run(async () => { await api.post(`/collector/jobs/${job.id}/stop`, {}); await reload(); })}>Остановить</button>}
        <button className={buttonClass} onClick={() => { repeatJob(job); }}>Повторить параметры</button>
        <button className={buttonClass} onClick={() => run(async () => setErrors(await api.get(`/collector/jobs/${job.id}/errors`)))}>Ошибки</button>
      </div><details><summary className="cursor-pointer text-sm">Сохранённые параметры</summary><pre className="overflow-auto text-xs p-2">{JSON.stringify(job.params, null, 2)}</pre></details>
    </article>)}</section></div>}
    {tab === 'results' && <section className={panelClass}>
      <h2 className="font-semibold text-lg">Результаты: {collectionSources.find(source => source.key === filters.source)?.name || 'все источники'}</h2>
      <nav aria-label="Источники результатов" className="flex flex-wrap gap-2">{[...collectionSources, { key: '', name: 'Все источники' }].map(source => <button key={source.key} type="button" aria-pressed={filters.source === source.key}
        className={`${buttonClass} ${filters.source === source.key ? 'bg-blue-700 text-white border-blue-700' : ''}`} onClick={() => setFilters(values => ({ ...values, source: source.key, job_id: '' }))}>{source.name}</button>)}</nav>
      <div className="grid md:grid-cols-4 gap-3">
        {[['q', 'Название / ИНН / сайт'], ['city', 'Город'], ['region', 'Регион'], ['okved', 'ОКВЭД']].map(([key, label]) => <Field key={key} title={label}><input className={inputClass} value={filters[key]} onChange={e => filter(key, e.target.value)}/></Field>)}

        <Field title="Результат сбора"><p className="py-2 text-sm">Только компании с найденным email</p></Field>
        <Field title="Статус компании"><select className={inputClass} value={filters.status} onChange={e => filter('status', e.target.value)}><option value="">Все</option><option value="new">Новый</option><option value="verified">Проверен</option><option value="excluded">Исключён</option></select></Field>
        <Field title="Запуск"><select className={inputClass} value={filters.job_id} onChange={e => filter('job_id', e.target.value)}><option value="">Вся база</option>{jobs.filter(job => !filters.source || job.params.sources?.some(source => resultSource(source) === filters.source)).map(j => <option key={j.id} value={j.id}>{j.name} · {date(j.created_at)}</option>)}</select></Field>
        <Field title="Сортировка"><select className={inputClass} value={filters.sort} onChange={e => filter('sort', e.target.value)}><option value="created_at">Дата добавления</option><option value="checked_at">Дата проверки</option><option value="name">Название</option><option value="city">Город</option></select></Field>
        <Check title="Только действующие (известный статус)" checked={filters.only_active} onChange={v => filter('only_active', v)}/>
        <Check title="Сначала новые / по убыванию" checked={filters.descending} onChange={v => filter('descending', v)}/>
      </div>
      <div className="flex flex-wrap items-center gap-2"><span className="text-sm" role="status" aria-live="polite">Выбрано: {selectionCount}{allSelected ? ' · со всех страниц' : ''}</span><button disabled={busy || !table.items.length} className={buttonClass} onClick={selectPage}>Выбрать страницу</button><button disabled={busy || !table.total} className={buttonClass} onClick={() => { setAllSelected(true); setSelected([]); setExcluded([]); }}>Выбрать все ({table.total})</button><button className={buttonClass} onClick={clearSelection}>Снять выбор</button><button disabled={busy || !selectionCount} className={buttonClass} onClick={() => download('csv')}>Экспорт CSV</button><button disabled={busy || !selectionCount} className={buttonClass} onClick={() => download('xlsx')}>Экспорт Excel</button><button className={buttonClass} onClick={() => run(loadTable)}>Обновить</button></div>
      <div className="rounded-lg bg-gray-50 p-3 flex flex-wrap items-center gap-3"><select aria-label="Кампания для контактов" className="rounded border text-sm" value={campaignId} onChange={e => setCampaignId(e.target.value)}><option value="">Только в базу контактов</option>{campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select><button disabled={busy || !selectionCount} className={`${buttonClass} bg-blue-700 text-white`} onClick={() => run(async () => { const r = await api.post('/collector/transfer', { ...selectionPayload(), campaign_id: campaignId ? Number(campaignId) : null }); setMessage(`Добавлено контактов: ${r.created}; уже есть: ${r.existing}; заблокировано: ${r.blocked}; добавлено в кампанию на паузе: ${r.enrolled}.`); })}>Добавить в базу Quickly</button><p className="text-xs text-gray-500">Отписавшиеся и заблокированные адреса пропускаются. Участники кампании добавляются на паузе.</p></div>
      <div className="w-full min-w-0 overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b text-left"><th>Выбор</th><th className="p-2">Компания</th><th>Город / ОКВЭД</th><th className="p-2">Email</th><th className="p-2">Телефоны</th><th>Источник</th></tr></thead><tbody>{table.items.map(c => <tr key={c.id} className="border-b"><td><input type="checkbox" aria-label={'Выбрать ' + c.name} checked={allSelected ? !excluded.includes(c.id) : selected.includes(c.id)} onChange={e => toggleSelected(c.id, e.target.checked)}/></td><td className="p-2"><button className="text-blue-700 text-left underline" onClick={() => openCompany(c.id)}>{c.name}</button><p className="text-xs text-gray-500">{c.inn || 'ИНН не установлен'}{c.needs_review ? ' · Требует проверки совпадения' : ''}</p></td><td>{c.city || '—'}<br/>{c.okved_main || 'Не установлен'}</td><td className="p-2 align-top min-w-[220px]">{c.contacts.filter(x => x.kind === 'email').map(x => <div className="break-all" key={x.id}>{x.value}{x.is_primary ? ' ★' : ''}</div>)}</td><td className="p-2 align-top min-w-[160px]">{c.contacts.some(x => x.kind === 'phone') ? c.contacts.filter(x => x.kind === 'phone').map(x => <div className="whitespace-nowrap" key={x.id}>{x.value}</div>) : '—'}</td><td>{c.sources.map(s => sources.find(x => x.key === s)?.name || s).join(', ')}</td></tr>)}</tbody></table>{!table.items.length && <p className="p-8 text-center text-gray-500">Компаний с найденным email по выбранным фильтрам пока нет.</p>}</div>
      <div className="flex items-center justify-between"><button disabled={page <= 1} className={buttonClass} onClick={() => setPage(p => p - 1)}>Назад</button><span className="text-sm">Всего: {table.total} · Страница {page} из {Math.max(1, Math.ceil(table.total / 50))}</span><button disabled={page * 50 >= table.total} className={buttonClass} onClick={() => setPage(p => p + 1)}>Далее</button></div>
    </section>}
    {tab === 'sources' && <section className={panelClass}><h2 className="font-semibold">Источники и ключи API</h2><p className="text-sm text-gray-500">Ключи хранятся с существующим шифрованием Quickly. Для платных API нужны право хранения, разрешение запросов и оба лимита бюджета. Запрос проверки ключа здесь не выполняется.</p>{sources.map(s => <div key={s.key} className="border rounded-lg p-4 flex flex-wrap justify-between items-center gap-3"><div><strong>{s.name}</strong><p className="text-sm">{states[s.state]} · {s.message}</p><p className="text-xs text-gray-500">Запросов за {s.budget_day || 'сегодня'}: {s.requests_used} / {s.daily_request_limit}. <a href={s.terms_url} target="_blank" rel="noreferrer" className="underline">Документация</a></p></div><button className={buttonClass} onClick={() => setSourceEdit({ ...s, api_key: '', credentials: '', clear_key: false })}>Настроить</button></div>)}</section>}
    {tab === 'consents' && <section className={panelClass}><h2 className="font-semibold">Реестр отказов</h2><p className="text-sm text-gray-500">Адреса из этого реестра исключаются из дальнейших отправок.</p><button className={buttonClass} onClick={() => consentForm('')}>Добавить отказ</button>{permissions.map(p => <div key={p.email} className="border-b p-2 text-sm"><strong>{p.email}</strong><p>{p.evidence || 'Отказ от писем'}</p></div>)}</section>}
    {tab === 'report' && <form className={panelClass} onSubmit={e => { e.preventDefault(); run(async () => { await api.put('/collector/settings/report', report); setMessage('Настройки отчётов сохранены.'); }); }}><h2 className="font-semibold">Отчёт администратору</h2><Check title="Отправлять отчёт после завершения новых запусков" checked={report.enabled} onChange={v => setReport(r => ({ ...r, enabled: v }))}/><Field title="Получатель отчёта"><input type="email" className={inputClass} value={report.recipient} onChange={e => setReport(r => ({ ...r, recipient: e.target.value }))}/></Field><Field title="Почта отправителя отчёта"><select className={inputClass} value={report.inbox_id || ''} onChange={e => setReport(r => ({ ...r, inbox_id: Number(e.target.value) || null }))}><option value="">Выберите SMTP-ящик</option>{accounts.map(a => <option key={a.inbox_id} value={a.inbox_id}>{a.email || a.smtp_username}</option>)}</select></Field><Check title="Прикладывать CSV только с разрешёнными к экспорту данными" checked={report.attach_csv} onChange={v => setReport(r => ({ ...r, attach_csv: v }))}/><p className="text-sm text-gray-500">По умолчанию выключено. Для отправки действуют аварийная остановка и дневной лимит выбранного ящика. Неопределённый результат после сбоя не повторяется автоматически.</p><button disabled={busy} className={buttonClass}>Сохранить настройки отчётов</button><Link className="text-blue-700 ml-3" to="/settings#backup">Резервное копирование Quickly</Link></form>}
    {tab === 'help' && <section className={panelClass}><h2 className="font-semibold">Начало работы</h2><ol className="list-decimal pl-5 space-y-2 text-sm"><li>Все источники находятся в «Новом сборе»: выберите Яндекс Карты, 2ГИС, Rusprofile, СПАРК, сайты или файл. В «Результатах» переключайтесь между источниками; выбор всех контактов и экспорт относятся к открытой группе.</li><li>Яндекс Карты, 2ГИС, Rusprofile и СПАРК по умолчанию работают через браузер на Mac. Для Rusprofile и СПАРК откройте окно входа, задайте фильтры в сервисе и вставьте ссылку на выборку. Сайты и файлы также доступны в этой форме.</li><li>В «Новом сборе» задайте параметры, основание обработки и начните сбор. Пауза и продолжение находятся в истории запусков.</li><li>Откройте результаты. Если точные фильтры скрыли компании с неизвестным ОКВЭД или статусом, снимите соответствующий фильтр.</li><li>Карточка компании показывает каждый контакт и URL обнаружения. Отказы от писем можно сохранить в реестре отказов.</li><li>Выделите компании для экспорта или передачи в Quickly. Контакты можно передать в общую базу или в выбранную кампанию на паузе.</li><li>Официальные API настраиваются в «Источники и API». СПАРК требует договорной документации; ФНС работает через импорт официальных файлов.</li></ol><p className="text-sm text-gray-500">Сайты, требующие JavaScript, закрытые страницы и CAPTCHA не обходятся. Каждый сайт: до четырёх публичных страниц, не чаще разрешений robots.txt. Прогресс и данные входят в штатные резервные копии.</p></section>}
    {detail && <div className="fixed inset-0 z-50 bg-black/40 flex justify-end"><section role="dialog" aria-modal="true" aria-label="Карточка компании" className="bg-white h-full overflow-auto w-full max-w-3xl p-6 space-y-4"><div className="flex justify-between gap-3"><h2 className="text-xl font-semibold">{detail.name}</h2><button className={buttonClass} onClick={() => setDetail(null)}>Закрыть</button></div><dl className="grid grid-cols-2 gap-2 text-sm">{['inn', 'kpp', 'ogrn', 'okved_main', 'okved_extra', 'okved_as_of', 'region', 'city', 'address', 'website', 'industry', 'employees', 'active', 'status'].map(key => <div key={key}><dt className="text-gray-500">{{inn:'ИНН',kpp:'КПП',ogrn:'ОГРН',okved_main:'Основной ОКВЭД',okved_extra:'Дополнительные ОКВЭД',okved_as_of:'ОКВЭД актуален на',region:'Регион',city:'Город',address:'Адрес',website:'Сайт',industry:'Отрасль',employees:'Сотрудников',active:'Действующая',status:'Статус'}[key]}</dt><dd className="break-words">{detail[key] === true ? 'Да' : detail[key] === false ? 'Нет' : String(detail[key] ?? '') || 'Не установлено'}</dd></div>)}</dl><div className="flex gap-2">{[['new','Новый'],['verified','Проверен'],['excluded','Исключён']].map(([value,label]) => <button key={value} className={buttonClass} onClick={() => run(async () => { await api.patch('/collector/companies/' + detail.id, {status:value}); await openCompany(detail.id); await loadTable(); })}>{label}</button>)}</div><h3 className="font-semibold">Контакты</h3>{detail.contacts.map(c => <div key={c.id} className="border rounded p-3 text-sm space-y-1"><strong className="break-all">{c.value}{c.is_primary ? ' ★' : ''}</strong><p>{c.kind === 'email' ? 'MX: ' + c.mx_status : c.kind === 'form' ? 'Контактная форма (не отправлялась)' : 'Телефон'}</p>{c.origins.map((o,i) => <p key={i} className="text-xs break-all">{o.source} · <a href={/^https?:\/\//.test(o.url) ? o.url : undefined} target="_blank" rel="noreferrer" className="underline">{o.url}</a></p>)}{c.kind === 'email' && <div className="flex gap-2"><button className={buttonClass} onClick={() => consentForm(c.value)}>Не писать на этот адрес</button><button className={buttonClass} onClick={() => run(async () => { await api.put(`/collector/contacts/${c.id}/primary`, {}); await openCompany(detail.id); })}>Основной email</button><button className={buttonClass} onClick={() => run(async () => { await api.del(`/collector/contacts/${c.id}`); await openCompany(detail.id); await reload(); })}>Удалить контакт и связанные данные</button></div>}</div>)}<h3 className="font-semibold">Происхождение полей</h3>{detail.observations.map(o => <details key={o.id} className="border rounded p-2 text-xs"><summary>{o.source} · {date(o.checked_at)}</summary><p>{o.url}</p><pre className="whitespace-pre-wrap break-all">{JSON.stringify(o.fields, null, 2)}</pre><p>Права: {o.rights.basis}; экспорт: {o.rights.export ? 'разрешён' : 'запрещён'}</p></details>)}<h3 className="font-semibold">История отказов</h3>{detail.consent_history.filter(e => e.status === 'refused').map(e => <p key={e.id} className="text-xs">{date(e.created_at)} · {e.email} · {e.evidence}</p>)}</section></div>}
    {consent && <div className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center p-4"><form role="dialog" aria-modal="true" aria-label="Отказ от писем" className={`${panelClass} w-full max-w-xl max-h-full overflow-auto`} onSubmit={e => {e.preventDefault();run(async () => {await api.post('/collector/consents', {...consent, status:'refused'}); setConsent(null); setMessage('Адрес добавлен в реестр отказов.'); if(detail) await openCompany(detail.id); await loadTable();});}}><h2 className="text-lg font-semibold">Не отправлять письма</h2><Field title="Email"><input type="email" required className={inputClass} value={consent.email} onChange={e => setConsent(c => ({...c, email:e.target.value}))}/></Field><Field title="Причина (необязательно)"><input className={inputClass} value={consent.evidence} onChange={e => setConsent(c => ({...c, evidence:e.target.value}))}/></Field><p className="text-sm">Адрес будет исключён из дальнейших отправок.</p><button className={buttonClass} disabled={busy}>Сохранить отказ</button><button type="button" className={`${buttonClass} ml-2`} onClick={() => setConsent(null)}>Отмена</button></form></div>}
    {sourceEdit && <div className="fixed inset-0 z-50 bg-black/40 flex justify-end"><form role="dialog" aria-modal="true" aria-label="Настройка источника" className="bg-white w-full max-w-2xl h-full overflow-auto p-6 space-y-4" onSubmit={e=>{e.preventDefault();run(async()=>{const {name,state,message,has_api_key,has_credentials,key,requests_used,budget_day,...data}=sourceEdit;await api.put('/collector/sources/'+key,data);setSourceEdit(null);await reload();setMessage('Настройки источника сохранены.');});}}><h2 className="text-xl font-semibold">{sourceEdit.name}</h2>{[['enabled','Включить источник'],['allow_store','Договор / условия разрешают хранение в CRM'],['allow_export','Договор / условия разрешают экспорт'],['paid_authorized','Разрешаю платные запросы в пределах заданного бюджета']].map(([key,title])=><Check key={key} title={title} checked={sourceEdit[key]} onChange={v=>setSourceEdit(s=>({...s,[key]:v}))}/>)}{[['api_key','API-ключ (пусто — сохранить прежний)'],['credentials','Пароль / секрет СПАРК (пусто — сохранить прежний)'],['terms_url','URL условий / документации'],['rights_basis','Договор / основание прав хранения и экспорта']].map(([key,title])=><Field key={key} title={title}><input className={inputClass} type={['api_key','credentials'].includes(key)?'password':'text'} autoComplete="off" value={sourceEdit[key]} onChange={e=>setSourceEdit(s=>({...s,[key]:e.target.value}))}/></Field>)}<Field title="Максимум запросов в сутки"><input type="number" min="0" max="100000" className={inputClass} value={sourceEdit.daily_request_limit} onChange={e=>setSourceEdit(s=>({...s,daily_request_limit:Number(e.target.value)}))}/></Field>{[['plan','Название тарифа'],['request_cost_rub','Стоимость одного запроса, ₽'],['daily_budget_rub','Максимальный расход в сутки, ₽'],['interval_seconds','Интервал запросов, секунд (1–60)'],['region_id','2ГИС: region_id'],['rubric_id','2ГИС: rubric_id (не ОКВЭД)'],['wsdl_url','СПАРК: URL WSDL'],['username','СПАРК: логин']].map(([key,title])=><Field key={key} title={title}><input className={inputClass} value={sourceEdit.config[key] ?? ''} onChange={e=>setSourceEdit(s=>({...s,config:{...s.config,[key]:e.target.value}}))}/></Field>)}{[['contacts_access','2ГИС: разрешаю платные контактные поля; доступ включён у ключа'],['employees_access','2ГИС: доступна платная численность'],['inn_access','2ГИС: доступен платный ИНН']].map(([key,title])=><Check key={key} title={title} checked={!!sourceEdit.config[key]} onChange={v=>setSourceEdit(s=>({...s,config:{...s.config,[key]:v}}))}/>)}<Check title="Удалить сохранённые ключи и секреты этого источника" checked={sourceEdit.clear_key} onChange={v=>setSourceEdit(s=>({...s,clear_key:v}))}/><p className="text-xs text-gray-500">СПАРК активируется после получения WSDL и описания методов по вашему договору. Неизвестные методы не вызываются.</p><button disabled={busy} className={`${buttonClass} bg-blue-700 text-white`}>Сохранить источник</button><button type="button" className={`${buttonClass} ml-2`} onClick={()=>setSourceEdit(null)}>Закрыть</button></form></div>}
    {errors && <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"><section role="dialog" aria-label="Ошибки сбора" className={`${panelClass} max-w-2xl max-h-full overflow-auto`}><h2 className="font-semibold">Ошибки и ограничения</h2>{errors.length ? errors.map(e=><p key={e.task_id} className="text-sm">{e.source} · {e.error} · попыток: {e.attempts}{e.due_at?' · '+date(e.due_at):''}</p>) : <p>Ошибок нет.</p>}<button className={buttonClass} onClick={()=>setErrors(null)}>Закрыть</button></section></div>}
    </div>
  </main>;
}
