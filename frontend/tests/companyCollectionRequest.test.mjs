import test from 'node:test';
import assert from 'node:assert/strict';
import { collectionRequest, resultSource } from '../src/components/local/companyCollectionRequest.js';

const params = { name: 'Поиск', cities: [' Тула ', '', 'Рязань'], query: 'Юристы', limit: 100,
  rights_confirmed: false, rights_basis: 'Fixture', websites: ['https://fixture.ru/'], only_email: false,
  parser_run: 'old-run', parser_checkpoint: 3, sources: ['old'] };

test('all four services use their existing browser endpoints and preserve shared fields', () => {
  for (const [source, parser] of [['yandex', 'yandex'], ['twogis', '2gis'], ['rusprofile', 'rusprofile'], ['spark', 'spark']]) {
    const request = collectionRequest(source, 'browser', params, { urls: 'https://rusprofile.ru/search?a=1,2\n', rubrics: '344; 5' });
    assert.equal(request.path, '/collector/local-parser/runs');
    assert.equal(request.body.source, parser);
    assert.deepEqual(request.body.cities, ['Тула', 'Рязань']);
    assert.deepEqual(request.body.urls, ['https://rusprofile.ru/search?a=1,2']);
    assert.deepEqual(request.body.rubrics, ['344', '5']);
    assert.equal(request.body.rights_confirmed, false);
    assert.equal(request.body.name, params.name);
    assert.equal(request.body.parser_run, undefined);
  }
});

test('websites and files have one selected source and retain the email requirement', () => {
  for (const source of ['website', 'import', 'fns']) {
    const request = collectionRequest(source, 'browser', params);
    assert.equal(request.path, source === 'website' ? '/collector/jobs' : '/collector/import');
    assert.equal(request.upload, source !== 'website');
    assert.deepEqual(request.body.sources, [source]);
    assert.deepEqual(request.body.websites, source === 'website' ? params.websites : []);
    assert.equal(request.body.only_email, true);
    assert.equal(request.body.parser_checkpoint, undefined);
    assert.equal(request.body.parser_run, undefined);
  }
});

test('API collection remains available without carrying old browser metadata', () => {
  const request = collectionRequest('twogis', 'api', params);
  assert.equal(request.path, '/collector/jobs');
  assert.deepEqual(request.body.sources, ['twogis']);
  assert.deepEqual(request.body.websites, []);
  assert.equal(request.body.parser_run, undefined);
});

test('history opens the matching result group', () => {
  for (const [source, group] of [['local_yandex', 'yandex'], ['2gis', 'twogis'], ['local_2gis', 'twogis'], ['local_rusprofile', 'rusprofile'], ['local_spark', 'spark'], ['website', 'website']]) {
    assert.equal(resultSource(source), group);
  }
  assert.equal(collectionRequest('rusprofile', 'api', params).path, '/collector/local-parser/runs');
  assert.throws(() => collectionRequest('unknown', 'browser', params), /Выберите/);
});
