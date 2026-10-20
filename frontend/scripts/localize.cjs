// Translate presentation output only. Comparisons, API payloads, editor values,
// templates and user-provided mail/contact content retain their original values.
module.exports = function localize({ types: t }) {
  const visibleAttributes = new Set(['title', 'placeholder', 'aria-label', 'alt', 'label']);
  const userFields = new Set(['name', 'display_name', 'email', 'subject', 'body', 'body_plain', 'body_html',
    'snippet', 'text', 'campaign_name', 'custom_data', 'company', 'city', 'okved', 'content']);
  function isUserContent(node) {
    if (!node || typeof node !== 'object') return false;
    if (t.isIdentifier(node) && userFields.has(node.name)) return true;
    if (t.isMemberExpression(node) && ((t.isIdentifier(node.property) && userFields.has(node.property.name)) ||
        (t.isStringLiteral(node.property) && userFields.has(node.property.value)))) return true;
    return Object.entries(node).some(([key, value]) => !['loc', 'start', 'end', 'extra'].includes(key) &&
      (Array.isArray(value) ? value.some(isUserContent) : value && typeof value === 'object' && isUserContent(value)));
  }
  function cleanText(value) {
    const lines = value.split(/\r\n|\n|\r/);
    let last = 0;
    lines.forEach((line, index) => { if (/[^ \t]/.test(line)) last = index; });
    let result = '';
    lines.forEach((line, index) => {
      let text = line.replace(/\t/g, ' ');
      if (index !== 0) text = text.replace(/^ +/, '');
      if (index !== lines.length - 1) text = text.replace(/ +$/, '');
      if (text) result += text + (index !== last ? ' ' : '');
    });
    return result;
  }
  function translateFallback(node, state) {
    if (t.isLogicalExpression(node)) {
      node.left = translateFallback(node.left, state);
      node.right = translateFallback(node.right, state);
    } else if (t.isConditionalExpression(node)) {
      node.consequent = translateFallback(node.consequent, state);
      node.alternate = translateFallback(node.alternate, state);
    } else if (t.isStringLiteral(node) && /[A-Za-z]/.test(node.value)) {
      state.used = true;
      return t.callExpression(state.translator, [node]);
    }
    return node;
  }
  return {
    visitor: {
      Program: {
        enter(path, state) {
          state.localize = !/\/EmailContent\.jsx$/.test(state.filename || '');
          state.translator = path.scope.generateUidIdentifier('translateUi');
          state.used = false;
        },
        exit(path, state) {
          if (state.used) path.unshiftContainer('body', t.importDeclaration([
            t.importSpecifier(state.translator, t.identifier('translate')),
          ], t.stringLiteral('/src/i18n/index.js')));
        },
      },
      JSXText(path, state) {
        if (!state.localize) return;
        const value = cleanText(path.node.value);
        if (!/[A-Za-zА-Яа-яЁё]/.test(value)) return;
        state.used = true;
        path.replaceWith(t.jsxExpressionContainer(t.callExpression(state.translator, [t.stringLiteral(value)])));
        path.skip();
      },
      JSXAttribute(path, state) {
        if (!state.localize || !(visibleAttributes.has(path.node.name.name) || (path.node.name.name === 'name' && ['Line','Area','Bar'].includes(path.parent.name?.name))) ) return;
        const current = t.isStringLiteral(path.node.value) ? path.node.value : t.isJSXExpressionContainer(path.node.value) ? path.node.value.expression : null;
        if (!current || t.isJSXEmptyExpression(current) || isUserContent(current)) return;
        state.used = true;
        path.node.value = t.jsxExpressionContainer(t.callExpression(state.translator, [current]));
        path.skip();
      },
      JSXExpressionContainer(path, state) {
        if (!state.localize || t.isJSXAttribute(path.parent) || t.isJSXEmptyExpression(path.node.expression) ) return;
        if (isUserContent(path.node.expression)) { path.node.expression = translateFallback(path.node.expression, state); return; }
        state.used = true;
        path.node.expression = t.callExpression(state.translator, [path.node.expression]);
      },
    },
  };
};
