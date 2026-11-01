import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../../api';

const normalize = value => value.toLocaleLowerCase('ru').replaceAll('ё', 'е');

export default function RegionPicker({ source, countryCode, catalogAvailable, values, onChange, disabled }) {
  const id = useId(), input = useRef(null);
  const [rows, setRows] = useState([]), [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false), [error, setError] = useState(''), [retry, setRetry] = useState(0);
  useEffect(() => {
    input.current?.setCustomValidity(values.length ? '' : 'Отметьте хотя бы одну область / регион.');
  }, [values.length]);
  useEffect(() => {
    if (!catalogAvailable) { setRows([]); return; }
    let cancelled = false;
    setLoading(true); setError('');
    const params = new URLSearchParams({ source, country_code: countryCode, kind: 'regions' });
    api.get('/collector/local-parser/catalog?' + params)
      .then(data => { if (!cancelled) setRows(data.sort((a, b) => a.name.localeCompare(b.name, 'ru'))); })
      .catch(err => { if (!cancelled) { setRows([]); setError(err.message); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [source, countryCode, catalogAvailable, retry]);
  const selected = rows.filter(row => values.includes(row.code));
  const count = selected.reduce((sum, row) => sum + row.source_cities, 0);
  const matches = rows.filter(row => normalize(row.name + ' ' + row.ascii).includes(normalize(query.trim())));
  const unavailable = selected.some(row => !row.source_cities);
  return <div className="space-y-3 min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-2 text-sm"><strong>Отметьте области / регионы</strong><span role="status">Выбрано регионов: {values.length} / 30</span></div>
    <label className="block text-sm font-medium" htmlFor={id}>Найти область / регион</label>
    <input id={id} ref={input} type="text" value={query} disabled={disabled} onChange={event => setQuery(event.target.value)} placeholder="Например: Свердловская область" className="block w-full rounded-md border bg-transparent p-2 text-sm" />
    {loading && <p role="status" className="text-sm">Загрузка областей / регионов…</p>}
    {!catalogAvailable && <p role="status" className="text-sm">Ожидание подключения локального парсера…</p>}
    {!loading && catalogAvailable && !error && <ul aria-label="Области и регионы" className="grid grid-cols-1 md:grid-cols-2 gap-1 p-2 max-h-56 overflow-y-auto rounded-md border bg-gray-50">
      {matches.map(row => <li key={row.code}><label className={`flex items-start gap-2 cursor-pointer rounded-md border p-2 text-sm ${values.includes(row.code) ? 'bg-teal-50 border-teal-500' : 'border-transparent hover:bg-gray-100'}`}>
        <input type="checkbox" aria-label={row.name} checked={values.includes(row.code)} className="mt-1 shrink-0" disabled={disabled || (!values.includes(row.code) && (values.length >= 30 || !row.source_cities))}
          onChange={event => onChange(event.target.checked ? [...values, row.code] : values.filter(code => code !== row.code))} />
        <span><span className="block">{row.name}</span><span className="block text-xs text-gray-500">{row.source_cities ? `${row.source_cities} населённых пунктов у источника` : 'Нет покрытия у этого источника'}</span></span>
      </label></li>)}
      {!matches.length && <li className="p-2 text-sm">Регион не найден в каталоге выбранной страны.</li>}
    </ul>}
    {error && <div role="alert" className="space-y-2 text-sm"><p>Не удалось загрузить области: {error}</p><button type="button" disabled={disabled} onClick={() => setRetry(value => value + 1)} className="rounded-md border px-3 py-2">Повторить загрузку областей</button></div>}
    {selected.length > 0 && <div className="rounded-md border p-3 space-y-2 text-sm" aria-label="План поиска по регионам">
      <p role="status"><strong>План: {count} населённых пунктов.</strong> Общий лимит карточек ниже действует на весь запуск.</p>
      {selected.map(row => <div key={row.code}><span className="font-medium">{row.name}</span><button type="button" aria-label={'Убрать регион ' + row.name} disabled={disabled} onClick={() => onChange(values.filter(code => code !== row.code))} className="ml-2 px-1">×</button><p className="text-xs text-gray-500">{row.sample.join(', ')}{row.source_cities > row.sample.length ? ' и другие' : ''}</p></div>)}
      {unavailable && <p role="alert" className="text-red-700">Один из регионов недоступен выбранному источнику. Снимите его галочку или выберите Яндекс Карты.</p>}
    </div>}
    <p className="text-xs text-gray-500">{source === '2gis' ? 'Обходятся города и проекты 2ГИС, привязанные к выбранному региону. Полное покрытие области не гарантируется; для большего охвата используйте Яндекс Карты.' : 'Обходятся населённые пункты выбранных регионов из GeoNames: от 500 жителей и административные центры. Полнота найденных компаний зависит от выдачи карт.'}</p>
  </div>;
}
