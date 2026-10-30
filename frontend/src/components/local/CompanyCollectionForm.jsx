import { useEffect, useState } from 'react';
import { api } from '../../api';

import { collectionSources } from './companyCollectionRequest';
import { countryLabels, parseCountryCommand } from './countryCollection';
import CityPicker from './CityPicker';

const input = 'block w-full rounded-md border border-gray-300 bg-transparent p-2 text-sm';
const button = 'rounded-md border border-gray-300 px-3 py-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed';
function Field({ title, children }) { return <label className="block text-sm font-medium space-y-1"><span>{title}</span>{children}</label>; }

export default function CompanyCollectionForm({ params, change, source, onSource, method, onMethod, options, onOptions, sources, file, onFile, busy, onSubmit, onAccess, onSettings }) {
  const [parser, setParser] = useState({ connected: false, runs: [], message: 'Проверка соединения…' });
  const [countries, setCountries] = useState([]), [countryError, setCountryError] = useState('');
  const [command, setCommand] = useState(''), [commandMessage, setCommandMessage] = useState('');
  const definition = collectionSources.find(item => item.key === source);
  const browser = Boolean(definition.parser && (method === 'browser' || source === 'rusprofile'));
  const maps = source === 'yandex' || source === 'twogis';
  const wholeCountry = maps && browser && params.scope === 'country';
  const country = countries.find(value => value.code === (params.country_code || 'RU'));
  const upload = source === 'import' || source === 'fns';
  const active = parser.runs.some(run => run.status === 'running');
  const apiSource = sources.find(item => item.key === source);
  const apiBlocked = !browser && definition.parser && apiSource?.state !== 'ready';
  useEffect(() => {
    let cancelled = false;
    const load = () => api.get('/collector/local-parser').then(value => { if (!cancelled) setParser(value); })
      .catch(error => { if (!cancelled) setParser({ connected: false, runs: [], message: error.message }); });
    load(); const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!browser || !maps || !parser.connected) return;
    let cancelled = false;
    setCountryError('');
    api.get('/collector/local-parser/catalog?kind=countries&source=' + definition.parser)
      .then(values => { if (!cancelled) setCountries(countryLabels(values)); })
      .catch(error => { if (!cancelled) setCountryError(error.message); });
    return () => { cancelled = true; };
  }, [browser, maps, definition.parser, parser.connected]);

  return <form onSubmit={onSubmit} className="rounded-xl border border-gray-200 bg-white p-5 space-y-5" aria-label="Новый сбор компаний">
    <fieldset className="space-y-2">
      <legend className="text-lg font-semibold mb-2">Источник данных</legend>
      <div className="flex flex-wrap gap-2">{collectionSources.map(item => <button key={item.key} type="button" aria-pressed={source === item.key}
        className={`${button} ${source === item.key ? 'bg-blue-700 text-white border-blue-700' : ''}`} onClick={() => onSource(item.key)}>{item.name}</button>)}</div>
    </fieldset>
    {browser && <p role="status" className="text-sm text-gray-500">{!parser.connected ? parser.message : !parser.parser_available ? 'Парсер недоступен. Проверьте его установку на Mac.' : active ? 'Один сбор уже выполняется. Его состояние и кнопка паузы — в «Истории запусков».' : 'Сбор через браузер на этом Mac. Подключение готово.'}</p>}
    {apiBlocked && <div role="status" className="rounded border p-3 text-sm">{apiSource?.message || 'Настройте подключение к API.'} <button type="button" className="underline" onClick={onSettings}>Настройки источников</button></div>}
    {maps && browser && <section className="rounded-lg border p-4 space-y-3" aria-label="География поиска">
      <div className="grid md:grid-cols-2 gap-4">
        <div className="text-sm font-medium"><label htmlFor="collection-country">Страна поиска</label><select id="collection-country" className={input} value={params.country_code || 'RU'} disabled={!countries.length} onChange={event => { const selected = countries.find(value => value.code === event.target.value); change('country_code', selected.code); change('country', selected.label); change('cities', []); setCommandMessage(''); }}>
          {!countries.length && <option value={params.country_code || 'RU'}>{countryError ? 'Каталог стран недоступен' : 'Загрузка стран…'}</option>}
          {countries.map(value => <option key={value.code} value={value.code}>{value.label}</option>)}
        </select></div>
        <div className="text-sm font-medium"><label htmlFor="collection-scope">Где искать</label><select id="collection-scope" className={input} value={params.scope || 'cities'} onChange={event => { change('scope', event.target.value); setCommandMessage(''); if (event.target.value === 'country' && params.limit === 100) change('limit', 10000); }}><option value="cities">Выбранные города</option><option value="country">Вся страна</option></select></div>
      </div>
      {!wholeCountry && <CityPicker key={source + (params.country_code || 'RU')} source={definition.parser} countryCode={params.country_code || 'RU'} catalogAvailable={parser.connected} allowManual={source !== 'twogis'} values={params.cities} onChange={values => change('cities', values)} disabled={busy} />}
      <div className="flex flex-wrap gap-2 items-end"><div className="flex-1 min-w-0 basis-64 text-sm"><label htmlFor="collection-command">Или задайте запрос одной строкой</label><input id="collection-command" className={input} value={command} placeholder="Собери почты строительных компаний по всему Узбекистану" onChange={event => setCommand(event.target.value)} /></div>
        <button type="button" className={button} disabled={!countries.length || !command.trim()} onClick={() => { try { const values = parseCountryCommand(command, countries); if (params.limit === 100) values.limit = 10000; Object.entries(values).forEach(([key, value]) => change(key, value)); setCommandMessage(`Выбрано: ${values.country}; тематика: ${values.query}. Проверьте параметры и нажмите «Начать поиск почт».`); } catch (error) { setCommandMessage(error.message); } }}>Заполнить параметры</button>
      </div>
      {commandMessage && <p role="status" className="text-sm">{commandMessage}</p>}
      {countryError && <p role="alert" className="text-sm text-red-700">{countryError}</p>}
      {wholeCountry && country && <p className="text-sm">План: {country.source_cities} населённых пунктов выбранной страны. Города будут обходиться автоматически. Лимит карточек ниже действует на весь запуск.</p>}
      {wholeCountry && country?.source_cities === 0 && <p role="alert" className="text-sm text-red-700">У этого источника нет покрытия выбранной страны. Выберите Яндекс Карты или другой источник.</p>}
      {wholeCountry && <p className="text-xs text-gray-500">География Яндекс Карт: <a className="underline" href="https://www.geonames.org/export/" target="_blank" rel="noreferrer">GeoNames, CC BY 4.0</a> — населённые пункты от 500 жителей и административные центры. Полнота компаний зависит от выдачи карт; прогресс обхода виден в истории.</p>}
    </section>}
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      <Field title="Название запуска"><input required className={input} value={params.name} onChange={event => change('name', event.target.value)}/></Field>
      <Field title="Сколько карточек проверить"><input required type="number" min="1" max="10000" className={input} value={params.limit} onChange={event => change('limit', Number(event.target.value))}/></Field>
      {maps && !browser && <CityPicker key={source + '-api'} source={definition.parser} countryCode={params.country_code || 'RU'} catalogAvailable={false} allowManual values={params.cities} onChange={values => change('cities', values)} required={false} disabled={busy} />}
      {(maps || (!browser && definition.parser)) && <Field title="Тематика / поисковая фраза"><input className={input} required={!browser || source !== 'twogis' || !options.rubrics.trim()} placeholder="Например: юридические компании" value={params.query} onChange={event => change('query', event.target.value)}/></Field>}
    </div>
    {browser && !maps && <div className="space-y-3">
      <p className="text-sm text-gray-500">Откройте {definition.name}, задайте нужные фильтры и вставьте ссылку на выборку или карточки компаний.</p>
      <button type="button" disabled={busy || active || !parser.connected} className={button} onClick={() => onAccess(definition.parser)}>Открыть окно входа в {definition.name}</button>
      <Field title="HTTPS-ссылки выборки / карточек, по одной на строке"><textarea rows="3" className={input} value={options.urls} placeholder="Пусто — использовать сохранённую выборку" onChange={event => onOptions('urls', event.target.value)}/></Field>
    </div>}
    {source === 'website' && <Field title="Сайты организаций — по одному URL в строке"><textarea required rows="4" className={input} placeholder="https://your-company.ru/" value={params.websites.join('\n')} onChange={event => change('websites', event.target.value.split('\n'))}/></Field>}
    {upload && <div className="space-y-2"><Field title={source === 'fns' ? 'Файл ФНС: XML' : 'Файл компаний: CSV / Excel'}><input key={source} required type="file" accept={source === 'fns' ? '.xml' : '.csv,.tsv,.xlsx'} className={input} onChange={event => onFile(event.target.files?.[0] || null)}/></Field>
      <p className="text-xs text-gray-500">{file?.name || 'Нужны название компании и email либо сайт для поиска почты. Телефоны сохраняются отдельно.'}</p></div>}
    <details className="rounded-lg border p-3 space-y-3"><summary className="cursor-pointer text-sm font-medium">Расширенные параметры</summary>
      {definition.parser && source !== 'rusprofile' && <Field title="Способ подключения"><select className={input} value={method} onChange={event => onMethod(event.target.value)}><option value="browser">Через браузер на Mac</option><option value="api">Через API сервиса</option></select></Field>}
      {browser ? <div className="grid md:grid-cols-3 gap-4">
        {source === 'twogis' && <Field title="Коды рубрик 2ГИС (необязательно)"><input className={input} value={options.rubrics} onChange={event => onOptions('rubrics', event.target.value)}/></Field>}
        <Field title="Задержка между карточками, секунд"><input type="number" min="0.5" max="60" step="0.5" className={input} value={options.delay} onChange={event => onOptions('delay', Number(event.target.value))}/></Field>
        <Field title="Тайм-аут задания, секунд"><input type="number" min="30" max="3600" className={input} value={options.timeout} onChange={event => onOptions('timeout', Number(event.target.value))}/></Field>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={options.visible} onChange={event => onOptions('visible', event.target.checked)}/>Показать Chrome при сборе</label>
      </div> : <div className="grid md:grid-cols-3 gap-4">
        <Field title="Страна"><input className={input} value={params.country} onChange={event => change('country', event.target.value)}/></Field>
        <Field title="Регионы через точку с запятой"><input className={input} value={params.regions.join('; ')} onChange={event => change('regions', event.target.value.split(';').map(value => value.trim()))}/></Field>
        <Field title="Точные коды ОКВЭД"><input className={input} placeholder="62.01; 69.10" value={params.okved.join('; ')} onChange={event => change('okved', event.target.value.split(';').map(value => value.trim()))}/></Field>
        {[['employees_min', 'Численность от'], ['employees_max', 'Численность до']].map(([key, title]) => <Field key={key} title={title}><input className={input} type="number" min="0" value={params[key] ?? ''} onChange={event => change(key, event.target.value === '' ? null : Number(event.target.value))}/></Field>)}
        {[['only_active', 'Только подтверждённо действующие'], ['exclude_existing', 'Исключать ранее найденные'], ['crawl_websites', 'Искать email на сайтах'], ['check_mx', 'Проверять DNS/MX']].map(([key, title]) => <label key={key} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={params[key]} onChange={event => change(key, event.target.checked)}/>{title}</label>)}
      </div>}
    </details>
    <p className="text-sm text-gray-500">В результаты попадают только компании с найденным email. Итоговое число может быть меньше количества проверенных карточек. Результаты каждого источника показываются отдельно.</p>
    <Field title="Основание права обработки и хранения"><input required maxLength="2000" className={input} value={params.rights_basis} onChange={event => change('rights_basis', event.target.value)} placeholder="Собственные данные / договор / разрешение владельца"/></Field>
    <label className="flex items-start gap-2 text-sm"><input required type="checkbox" checked={params.rights_confirmed} onChange={event => change('rights_confirmed', event.target.checked)}/>Подтверждаю права на хранение и обработку выбранных данных</label>
    <button type="submit" disabled={busy || !params.rights_confirmed || apiBlocked || (wholeCountry && (!country || !country.source_cities)) || (browser && (!parser.connected || !parser.parser_available || active))} className={`${button} bg-blue-700 text-white`}>Начать поиск почт</button>
  </form>;
}
