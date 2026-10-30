const normalize = value => String(value).normalize('NFKC').toLowerCase().replaceAll('ё', 'е');
const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const stem = word => word.length > 4 ? word.replace(/(?:ия|ая|яя|а|я)$/, '') : word;

export function countryLabels(countries) {
  const names = new Intl.DisplayNames(['ru'], { type: 'region' });
  return countries.map(country => ({ ...country, label: names.of(country.code) || country.name }))
    .sort((a, b) => a.label.localeCompare(b.label, 'ru'));
}

function distance(a, b) {
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(row[j - 1] + 1, previous[j] + 1, previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    previous = row;
  }
  return previous[b.length];
}

export function parseCountryCommand(command, countries) {
  const text = normalize(command).replace(/[^\p{L}\p{N}\s-]/gu, ' ');
  const found = [];
  for (const country of countries) {
    for (const name of [country.label, country.name, ...(country.code === 'US' ? ['США'] : [])].filter(Boolean)) {
      const words = normalize(name).match(/\p{L}+/gu) || [];
      const pattern = words.map(word => escape(stem(word)) + (word.length > 4 ? '\\p{L}{0,4}' : '')).join('[\\s-]+');
      const match = pattern && new RegExp('(?<!\\p{L})' + pattern + '(?!\\p{L})', 'u').exec(text);
      if (match) { found.push({ country, start: match.index, end: match.index + match[0].length, score: 0 }); break; }
    }
  }
  if (!found.length) {
    for (const word of text.matchAll(/\p{L}+/gu)) {
      for (const country of countries) {
        const name = normalize(country.label || '');
        if (name.length < 6 || name.includes(' ')) continue;
        const score = Math.min(...[word[0], word[0].slice(0, -1), word[0].slice(0, -2)].map(value => distance(value, name)));
        if (score <= 2) found.push({ country, start: word.index, end: word.index + word[0].length, score });
      }
    }
  }
  found.sort((a, b) => a.score - b.score || (b.end - b.start) - (a.end - a.start));
  const exact = found.filter(value => value.score === 0 && !found.some(other => other.score === 0 && other.country.code !== value.country.code && other.start <= value.start && other.end >= value.end && other.end - other.start > value.end - value.start));
  if (new Set(exact.map(value => value.country.code)).size > 1) throw new Error('В запросе несколько стран. Запускайте поиск отдельно для каждой страны.');
  if (!found.length || (found[1] && found[0].score === found[1].score && found[0].country.code !== found[1].country.code && found[0].end - found[0].start === found[1].end - found[1].start)) {
    throw new Error('Не удалось однозначно определить страну. Выберите её в списке и укажите тематику.');
  }
  const match = found[0];
  const stop = new Set(['собери', 'соберите', 'собрать', 'найди', 'найдите', 'найти', 'мне', 'пожалуйста', 'все', 'всех', 'всю', 'всей', 'всему', 'всем', 'всего', 'весь', 'почты', 'почту', 'почта', 'email', 'emails', 'электронные', 'электронных', 'адреса', 'адресов', 'по', 'во', 'в', 'на', 'стране', 'страны', 'территории', 'целиком', 'полностью']);
  const query = (text.slice(0, match.start) + ' ' + text.slice(match.end)).split(/\s+/).filter(word => word && !stop.has(word)).join(' ');
  if (!query) throw new Error('Укажите тематику: например, строительные компании.');
  return { country_code: match.country.code, country: match.country.label, query, scope: 'country', cities: [] };
}
