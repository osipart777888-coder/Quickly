import russian from './ru.json';

export const language = localStorage.getItem('quicklyLanguage') || 'ru';
document.documentElement.lang = language;
const reverse = Object.fromEntries(Object.entries(russian).map(([en, ru]) => [ru, en]));
const templates = Object.entries(russian).filter(([key]) => /\{\{\d+\}\}/.test(key)).sort((a,b)=>b[0].replace(/\{\{\d+\}\}/g,'').length-a[0].replace(/\{\{\d+\}\}/g,'').length).map(([en, ru]) => {
  const slots = [];
  const pattern = en.split(/(\{\{\d+\}\})/).map(part => {
    const match = /^\{\{(\d+)\}\}$/.exec(part);
    if (match) { slots.push(match[1]); return '([\\s\\S]*?)'; }
    return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('');
  return { regex: new RegExp('^'+pattern+'$'), slots, ru };
});
export function translate(value) {
  if (typeof value !== 'string') return value;
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (language === 'en') return reverse[normalized] || value;
  if (russian[normalized]) return (/^\s/.test(value)?' ':'') + russian[normalized] + (/\s$/.test(value)?' ':'');
  for (const template of templates) {
    const match = template.regex.exec(normalized);
    if (match) return template.ru.replace(/\{\{(\d+)\}\}/g, (_, n) => match[template.slots.indexOf(n)+1] ?? '');
  }
  return value;
}
export function switchLanguage() {
  localStorage.setItem('quicklyLanguage', language === 'ru' ? 'en' : 'ru');
  location.reload();
}
