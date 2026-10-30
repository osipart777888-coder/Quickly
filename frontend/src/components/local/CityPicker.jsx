import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../../api';

const key = value => String(value).trim().replace(/\s+/g, ' ').toLocaleLowerCase('ru');

export default function CityPicker({ source, countryCode, catalogAvailable, allowManual, values, onChange, required = true, disabled = false }) {
  const id = useId();
  const input = useRef(null);
  const container = useRef(null);
  const [query, setQuery] = useState('');
  const [options, setOptions] = useState([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const suggestions = options.filter(city => !values.some(value => key(value) === key(city.name))).slice(0, 30);

  useEffect(() => {
    input.current?.setCustomValidity(required && !values.length ? 'Выберите город из списка или нажмите «Добавить город».' : '');
  }, [required, values.length]);

  useEffect(() => {
    if (!catalogAvailable || !open) { setLoading(false); return; }
    let cancelled = false;
    setLoading(true);
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ source, country_code: countryCode, q: query.trim(), kind: 'cities' });
      api.get('/collector/local-parser/catalog?' + params)
        .then(rows => { if (!cancelled) { setOptions(rows); setActive(-1); setError(''); } })
        .catch(err => { if (!cancelled) { setOptions([]); setError(err.message); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [source, countryCode, catalogAvailable, query, open]);

  function add(city) {
    let name = typeof city === 'string' ? city.trim().replace(/\s+/g, ' ') : city.name;
    if (!name) return;
    if (values.length >= 30) { setError('Можно выбрать до 30 городов. Для общего обхода используйте режим «Вся страна».'); return; }
    if (!allowManual && typeof city === 'string') {
      const found = options.find(option => key(option.name) === key(name) || key(option.code) === key(name));
      if (!found) { setError('Выберите город из списка 2ГИС.'); setOpen(true); return; }
      name = found.name;
    }
    if (!values.some(value => key(value) === key(name))) onChange([...values, name]);
    setQuery(''); setActive(-1); setError(''); setOpen(false);
    input.current?.focus();
  }

  return <div ref={container} className="space-y-2 min-w-0" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <label htmlFor={id} className="block text-sm font-medium">Город для поиска</label>
    <div className="flex flex-wrap gap-2 items-start">
      <div className="relative min-w-0 flex-1 basis-64">
        <input ref={input} id={id} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={id + '-list'}
          aria-activedescendant={open && active >= 0 && suggestions[active] ? id + '-option-' + active : undefined}
          autoComplete="off" maxLength={200} required={required && !values.length} disabled={disabled} value={query}
          className="block w-full rounded-md border border-gray-300 bg-transparent p-2 text-sm" placeholder="Начните вводить название города…"
          onFocus={() => setOpen(true)} onChange={event => { setQuery(event.target.value); setOptions([]); setActive(-1); setError(''); setOpen(true); }}
          onKeyDown={event => {
            if (event.key === 'Escape') { setOpen(false); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              event.preventDefault(); setOpen(true);
              setActive(current => Math.max(0, Math.min(suggestions.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1))));
            }
            if (event.key === 'Enter') { event.preventDefault(); add(active >= 0 && suggestions[active] ? suggestions[active] : query); }
          }} />
        {open && <div className="absolute top-full left-0 right-0 z-20 mt-1 rounded-md border bg-white shadow-lg">
          <ul id={id + '-list'} role="listbox" aria-label="Подсказки городов" className="max-h-52 overflow-y-auto py-1 text-sm">
            {!loading && suggestions.map((city, index) => <li key={city.code} id={id + '-option-' + index} role="option" aria-selected={index === active}
              className={`cursor-pointer px-3 py-2 hover:bg-gray-100 ${index === active ? 'bg-gray-100' : ''}`}
              onMouseDown={event => event.preventDefault()} onClick={() => add(city)}>{city.name}</li>)}
          </ul>
          {loading && <p role="status" className="p-3 text-sm text-gray-500">Загрузка городов…</p>}
          {!loading && !suggestions.length && <p className="p-3 text-sm text-gray-500">{allowManual ? 'Введите название и нажмите «Добавить город» или Enter.' : 'Город не найден. Уточните название или проверьте подключение к каталогу.'}</p>}
        </div>}
      </div>
      <button type="button" disabled={disabled || !query.trim() || (!allowManual && loading)} onClick={() => add(query)} className="rounded-md border border-gray-300 px-3 py-2 text-sm disabled:opacity-50">Добавить город</button>
    </div>
    {values.length > 0 && <ul aria-label="Выбранные города" className="flex flex-wrap gap-2">{values.map((city, index) => <li key={city + index} className="flex items-center gap-2 rounded-md border bg-gray-50 px-2 py-1 text-sm"><span>{city}</span><button type="button" disabled={disabled} aria-label={'Убрать город ' + city} onClick={() => onChange(values.filter((_, i) => i !== index))} className="px-1">×</button></li>)}</ul>}
    <p className="text-xs text-gray-500">{allowManual ? 'Выберите подсказку или введите город вручную. Пробелы в названии сохраняются. Можно добавить несколько городов.' : 'Выберите город из каталога 2ГИС. Можно добавить несколько городов.'}</p>
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </div>;
}
