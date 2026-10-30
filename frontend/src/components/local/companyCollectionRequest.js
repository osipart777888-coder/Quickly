export const collectionSources = [
  { key: 'yandex', name: 'Яндекс Карты', parser: 'yandex' },
  { key: 'twogis', name: '2ГИС', parser: '2gis' },
  { key: 'rusprofile', name: 'Rusprofile', parser: 'rusprofile' },
  { key: 'spark', name: 'СПАРК', parser: 'spark' },
  { key: 'website', name: 'Сайты организаций' },
  { key: 'import', name: 'CSV / Excel' },
  { key: 'fns', name: 'Файл ФНС' },
];
export function resultSource(source) {
  return ({ local_yandex: 'yandex', local_2gis: 'twogis', '2gis': 'twogis', local_spark: 'spark', local_rusprofile: 'rusprofile' })[source] || source || '';
}

const jobFields = ['name', 'country', 'regions', 'cities', 'okved', 'query', 'employees_min', 'employees_max',
  'limit', 'only_active', 'exclude_existing', 'crawl_websites', 'check_mx', 'rights_confirmed', 'rights_basis'];
const split = value => String(value || '').split(/[;,\n]+/).map(item => item.trim()).filter(Boolean);

export function collectionRequest(source, method, params, options = {}) {
  const definition = collectionSources.find(item => item.key === source);
  if (!definition) throw new Error('Выберите источник данных');
  if (definition.parser && (method === 'browser' || source === 'rusprofile')) {
    return { path: '/collector/local-parser/runs', upload: false, body: {
      source: definition.parser, name: params.name, query: params.query,
      scope: ['yandex', 'twogis'].includes(source) ? params.scope || 'cities' : 'cities',
      country_code: ['yandex', 'twogis'].includes(source) ? params.country_code || 'RU' : 'RU',
      country: ['yandex', 'twogis'].includes(source) ? params.country || '' : 'Россия',
      cities: (params.cities || []).map(value => value.trim()).filter(Boolean), limit: params.limit,
      rubrics: split(options.rubrics), urls: String(options.urls || '').split('\n').map(value => value.trim()).filter(Boolean),
      timeout: options.timeout ?? 900, delay: options.delay ?? 1, visible: Boolean(options.visible),
      crawl_websites: true, rights_confirmed: params.rights_confirmed, rights_basis: params.rights_basis,
    } };
  }
  const upload = source === 'import' || source === 'fns';
  return { path: upload ? '/collector/import' : '/collector/jobs', upload, body: {
    ...Object.fromEntries(jobFields.filter(key => params[key] !== undefined).map(key => [key, params[key]])),
    sources: [source], only_email: true, websites: source === 'website' ? params.websites || [] : [],
  } };
}
