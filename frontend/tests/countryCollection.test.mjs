import test from 'node:test';
import assert from 'node:assert/strict';
import { countryLabels, parseCountryCommand } from '../src/components/local/countryCollection.js';
import { collectionRequest } from '../src/components/local/companyCollectionRequest.js';

const countries = countryLabels([{ code: 'UZ', name: 'Uzbekistan' }, { code: 'KZ', name: 'Kazakhstan' },
  { code: 'RU', name: 'Russia' }, { code: 'TR', name: 'Turkey' }, { code: 'US', name: 'United States' }]);

for (const [text, code] of [['Собери все почты строительных компаний по всему Узбекистану', 'UZ'],
  ['собери строительные компании по всему узбексистану', 'UZ'], ['Найди мне строительные компании Казахстана', 'KZ'],
  ['Собери строительные компании Турции', 'TR'], ['Найди строительные компании США', 'US']]) {
  test('country command: ' + text, () => {
    const parsed = parseCountryCommand(text, countries);
    assert.equal(parsed.country_code, code);
    assert.equal(parsed.scope, 'country');
    assert.deepEqual(parsed.cities, []);
    assert.match(parsed.query, /строительн/);
    const request = collectionRequest('yandex', 'browser', { ...parsed, limit: 100 });
    assert.equal(request.body.country_code, code);
    assert.equal(request.body.scope, 'country');
  });
}

test('unknown, ambiguous country and missing topic require an explicit correction', () => {
  assert.throws(() => parseCountryCommand('собери компании на Марсе', countries), /страну/);
  assert.throws(() => parseCountryCommand('компании Узбекистана и Казахстана', countries), /несколько стран/);
  assert.throws(() => parseCountryCommand('собери все почты по Узбекистану', countries), /тематику/);
});

test('non-map sources do not inherit foreign geography from a previous search', () => {
  const request = collectionRequest('rusprofile', 'browser', { scope: 'country', country_code: 'UZ', country: 'Узбекистан', cities: [] });
  assert.equal(request.body.country_code, 'RU');
  assert.equal(request.body.scope, 'cities');
});
