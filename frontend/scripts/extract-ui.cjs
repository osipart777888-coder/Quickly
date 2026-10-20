const fs = require('fs');
const path = require('path');
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;
const phrases = new Set();
function add(value) {
  value = value.replace(/\s+/g, ' ').trim();
  if (/[a-zA-Z]/.test(value) && value.length > 1 && !/^(https?:|\/|@keyframes|Authorization:|#)/.test(value)) phrases.add(value);
}
function walk(folder) {
  for (const name of fs.readdirSync(folder)) {
    const file = path.join(folder, name);
    if (fs.statSync(file).isDirectory()) { walk(file); continue; }
    if (!file.endsWith('.jsx')) continue;
    const ast = parser.parse(fs.readFileSync(file, 'utf8'), {sourceType:'module', plugins:['jsx']});
    traverse(ast, {
      JSXText(p) { add(p.node.value); },
      StringLiteral(p) {
        const parent = p.parent;
        if ((parent.type === 'JSXAttribute' && ['title','placeholder','aria-label','alt','label'].includes(parent.name.name)) ||
            (parent.type === 'ObjectProperty' && ['label','title','description','message'].includes(parent.key.name)) ||
            (p.findParent(p => p.isJSXExpressionContainer()) && !p.findParent(p => p.isJSXAttribute()))) add(p.node.value);
      },
      TemplateLiteral(p) {
        if (p.findParent(p => p.isJSXExpressionContainer())) add(p.node.quasis.map((q,i) => q.value.cooked + (i < p.node.expressions.length ? `{{${i}}}` : '')).join(''));
      },
    });
  }
}
walk(path.resolve(__dirname,'../src'));
fs.writeFileSync(path.resolve(__dirname,'../src/i18n/catalog.json'), JSON.stringify([...phrases].sort(), null, 2)+'\n');
console.log(`${phrases.size} UI phrases extracted`);
