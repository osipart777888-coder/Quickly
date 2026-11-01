import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../../api';

const key = value => String(value).trim().replace(/\s+/g, ' ').toLocaleLowerCase('ru');

export default function CityPicker({ source, countryCode, catalogAvailable, allowManual, values, onChange, required = true, disabled = false }) {
  const id = useId();
  const input = useRef(null);
  const [query, setQuery] = useState('');
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [selectionError, setSelectionError] = useState('');
  const [retry, setRetry] = useState(0);
  const suggestions = options.slice(0, 100);

  useEffect(() => {
    input.current?.setCustomValidity(required && !values.length ? 'Отметьте хотя бы один город галочкой в списке.' : '');
  }, [required, values.length]);

  useEffect(() => {
    if (!catalogAvailable) { setLoading(false); setOptions([]); return; }
    let cancelled = false;
    setLoading(true);
    setCatalogError('');
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ source, country_code: countryCode, q: query.trim(), kind: 'cities' });
      api.get('/collector/local-parser/catalog?' + params)
        .then(rows => { if (!cancelled) setOptions(rows); })
        .catch(error => { if (!cancelled) { setOptions([]); setCatalogError(error.message); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [source, countryCode, catalogAvailable, query, retry]);

  function select(name, checked) {
    const selected = values.some(value => key(value) === key(name));
    if (checked && !selected) {
      if (values.length >= 30) { setSelectionError('Можно выбрать до 30 городов. Для общего обхода используйте режим «Вся страна».'); return; }
      onChange([...values, name]);
    } else if (!checked && selected) {
      onChange(values.filter(value => key(value) !== key(name)));
    }
    setSelectionError('');
  }

  function addQuery() {
    const name = query.trim().replace(/\s+/g, ' ');
    if (!name) return;
    const found = options.find(city => key(city.name) === key(name) || key(city.code) === key(name));
    if (!allowManual && (!found || loading)) { setSelectionError('Найдите город и отметьте его галочкой в списке 2ГИС.'); return; }
    if (values.length >= 30 && !values.some(value => key(value) === key(found?.name || name))) {
      setSelectionError('Можно выбрать до 30 городов. Для общего обхода используйте режим «Вся страна».'); return;
    }
    select(found?.name || name, true);
    setQuery('');
  }

  return <div className="space-y-3 min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span className="font-medium">Отметьте города галочками</span>
      <span role="status" aria-live="polite">Выбрано городов: {values.length} / 30</span>
    </div>
    {values.length > 0 && <ul aria-label="Выбранные города" className="flex flex-wrap gap-2">{values.map((city, index) => <li key={city + index} className="flex items-center gap-2 rounded-md border bg-gray-50 px-2 py-1 text-sm"><span>{city}</span><button type="button" disabled={disabled} aria-label={'Убрать город ' + city} onClick={() => select(city, false)} className="px-1">×</button></li>)}</ul>}
    <div>
      <label htmlFor={id} className="block text-sm font-medium mb-1">Найти город</label>
      <div className="flex flex-wrap gap-2">
        <input ref={input} id={id} type="text" autoComplete="off" maxLength={200} disabled={disabled} value={query}
          aria-controls={catalogAvailable ? id + '-list' : undefined}
          className="min-w-0 flex-1 basis-64 rounded-md border border-gray-300 bg-transparent p-2 text-sm" placeholder="Например: Екатеринбург или Нижний Новгород"
          onChange={event => { setQuery(event.target.value); setOptions([]); setSelectionError(''); }}
          onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addQuery(); } }} />
        {query && <button type="button" disabled={disabled} onClick={() => { setQuery(''); setSelectionError(''); }} className="rounded-md border px-3 py-2 text-sm">Сбросить поиск города</button>}
        {allowManual && <button type="button" disabled={disabled || !query.trim()} onClick={addQuery} className="rounded-md border px-3 py-2 text-sm disabled:opacity-50">Добавить введённый город</button>}
      </div>
    </div>
    {catalogAvailable && <div className="rounded-md border bg-gray-50">
      {loading && <p role="status" className="p-3 text-sm">Загрузка городов…</p>}
      {!loading && !catalogError && <>
        <ul id={id + '-list'} aria-label="Список городов" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1 p-2 max-h-56 overflow-y-auto">
          {suggestions.map(city => {
            const checked = values.some(value => key(value) === key(city.name));
            return <li key={city.code} className="min-w-0"><label className={`flex items-center gap-2 cursor-pointer rounded-md border p-2 text-sm ${checked ? 'bg-teal-50 border-teal-500' : 'border-transparent hover:bg-gray-100'}`}>
              <input type="checkbox" checked={checked} disabled={disabled} className="shrink-0" onChange={event => select(city.name, event.target.checked)} />
              <span className="break-words min-w-0">{city.name}</span>
            </label></li>;
          })}
        </ul>
        {options.length > suggestions.length && <p className="border-t p-2 text-xs text-gray-500">Показаны первые {suggestions.length} городов. Поиск выше ищет по всему каталогу.</p>}
        {!suggestions.length && <p className="p-3 text-sm">{allowManual ? 'В каталоге не найдено. Можно добавить введённое название вручную.' : 'Город не найден в каталоге 2ГИС этой страны. Уточните название или выберите Яндекс Карты.'}</p>}
      </>}
      {catalogError && <div role="alert" className="p-3 space-y-2 text-sm"><p>Не удалось загрузить города: {catalogError}</p><button type="button" disabled={disabled} className="rounded-md border px-3 py-2" onClick={() => setRetry(value => value + 1)}>Повторить загрузку городов</button></div>}
    </div>}
    {!catalogAvailable && <p role="status" className="text-sm text-gray-500">{allowManual ? 'Каталог пока недоступен. Можно ввести название и нажать «Добавить введённый город».' : 'Ожидание подключения локального парсера для загрузки городов 2ГИС.'}</p>}
    <p className="text-xs text-gray-500">Города выбираются галочками в списке выше. Можно выбрать несколько, до 30 за запуск. Поиск по названию сохраняет уже выбранные города.</p>
    {selectionError && <p role="alert" className="text-sm text-red-700">{selectionError}</p>}
  </div>;
}
