const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const babel=require('@babel/core');
const russian=require('../src/i18n/ru.json');
function translator(lang){
  const context={russian,localStorage:{getItem:()=>lang},document:{documentElement:{}}};
  const source=fs.readFileSync(require.resolve('../src/i18n/index.js'),'utf8').replace("import russian from './ru.json';",'').replaceAll('export ','');
  vm.runInNewContext(source+'\nthis.result=translate;',context);
  return context.result;
}
test('Russian presentation preserves arbitrary user text and template values',()=>{
  const tr=translator('ru');
  assert.equal(tr('Save'),'Сохранить');
  assert.equal(tr('Client  custom\nmessage'),'Client  custom\nmessage');
  assert.equal(tr('Row 14: Неверный email'),'Строка 14: Неверный email');
  assert.equal(tr('Added: 1; updated: 2; duplicates: 3; invalid: 4. Строка 5'), 'Добавлено: 1; обновлено: 2; дубликатов: 3; некорректных: 4. Строка 5');
  assert.equal(translator('en')('Save'),'Save');
});
test('Localization never rewrites routes, form values or user content',()=>{
  const source=`const route='/api/leads';const Example=({lead})=><div className="text-sm"><input name="status" value="active"/><span>{lead.name}</span><span>{lead.custom_data?.city || 'No date'}</span><button title="Save">Save</button></div>`;
  const result=babel.transformSync(source,{filename:'Example.jsx',configFile:false,plugins:[require('./localize.cjs')],parserOpts:{plugins:['jsx']}}).code;
  assert.ok(result.includes("route = '/api/leads'"));
  assert.ok(result.includes('value="active"'));
  assert.ok(result.includes('{lead.name}'));
  assert.match(result,/lead\.custom_data\?\.city \|\| \w+\('No date'\)/);
  assert.match(result,/title=\{\w+\("Save"\)\}/);
});
