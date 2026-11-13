/* Render Marked's token tree into DOM nodes. Never insert model-produced HTML.
 * Marked 17.0.5 is vendored locally under static/vendor/marked (MIT).
 */
(function () {
  'use strict';

  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
    copy: '©', reg: '®', ndash: '–', mdash: '—', hellip: '…', times: '×', divide: '÷',
    le: '≤', ge: '≥', ne: '≠', plusmn: '±', euro: '€', pound: '£', yen: '¥', cent: '¢' };
  function textValue(text) {
    return String(text || '').replace(/&(#x[\da-f]+|#\d+|[a-z]+);/gi, (raw, name) => {
      if (name[0] !== '#') return entities[name] ?? raw;
      const value = name[1].toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : Number(name.slice(1));
      return value > 0 && value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff) ? String.fromCodePoint(value) : '\ufffd';
    });
  }

  function safeLink(href) {
    href = textValue(href);
    if (typeof href !== 'string' || /[\u0000-\u0020\u007f]/.test(href)) return null;
    try {
      const url = new URL(href);
      return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function appendTokens(tokens, parent, depth = 0) {
    if (depth > 40) {
      parent.append(document.createTextNode((tokens || []).map(token => token.text || token.raw || '').join('')));
      return;
    }
    const children = (token, node) => {
      if (token.tokens) appendTokens(token.tokens, node, depth + 1);
      else node.append(document.createTextNode(textValue(token.text)));
    };
    for (const token of tokens || []) {
      let node;
      switch (token.type) {
        case 'space': continue;
        case 'heading':
          node = element(`h${Math.min(6, Math.max(1, token.depth))}`);
          children(token, node);
          break;
        case 'paragraph':
          node = element('p');
          children(token, node);
          break;
        case 'text':
          if (token.tokens) appendTokens(token.tokens, parent, depth + 1);
          else parent.append(document.createTextNode(textValue(token.text)));
          continue;
        case 'strong': case 'em': case 'del':
          node = element(token.type);
          children(token, node);
          break;
        case 'codespan': node = element('code', token.text); break;
        case 'code': {
          node = element('div', undefined, 'markdown-code-block');
          if (token.lang) node.append(element('div', token.lang.split(/\s+/)[0], 'code-language'));
          const pre = element('pre');
          pre.append(element('code', token.text));
          node.append(pre);
          break;
        }
        case 'br': node = element('br'); break;
        case 'hr': node = element('hr'); break;
        case 'blockquote':
          node = element('blockquote');
          appendTokens(token.tokens, node, depth + 1);
          break;
        case 'list':
          node = element(token.ordered ? 'ol' : 'ul');
          if (token.ordered && Number.isSafeInteger(token.start) && token.start > 0) node.start = token.start;
          for (const item of token.items) {
            const li = element('li');
            if (item.task) {
              li.className = 'task-list-item';
              const checkbox = element('input');
              checkbox.type = 'checkbox';
              checkbox.disabled = true;
              checkbox.checked = !!item.checked;
              checkbox.setAttribute('aria-label', item.checked ? 'Completed' : 'Not completed');
              li.append(checkbox);
            }
            appendTokens(item.tokens, li, depth + 1);
            node.append(li);
          }
          break;
        case 'table': {
          node = element('div', undefined, 'markdown-table-wrap');
          node.tabIndex = 0;
          node.setAttribute('role', 'region');
          node.setAttribute('aria-label', 'Answer table; scroll horizontally for more columns');
          const table = element('table', undefined, 'markdown-table');
          const head = element('thead'), body = element('tbody'), header = element('tr');
          const cell = (value, tag, index) => {
            const result = element(tag);
            if (tag === 'th') result.scope = 'col';
            const align = token.align?.[index];
            if (['left', 'right', 'center'].includes(align)) result.className = `align-${align}`;
            children(value, result);
            return result;
          };
          token.header.forEach((value, index) => header.append(cell(value, 'th', index)));
          head.append(header);
          for (const values of token.rows) {
            const row = element('tr');
            values.forEach((value, index) => row.append(cell(value, 'td', index)));
            body.append(row);
          }
          table.append(head, body);
          node.append(table);
          break;
        }
        case 'link': {
          const href = safeLink(token.href);
          node = element(href ? 'a' : 'span');
          children(token, node);
          if (href) {
            node.href = href;
            node.target = '_blank';
            node.rel = 'noopener noreferrer';
            if (token.title) node.title = token.title;
          }
          break;
        }
        case 'image': {
          // Keep remote images as explicit links; never auto-fetch model URLs.
          const href = safeLink(token.href);
          node = element(href ? 'a' : 'span', token.text || 'View image');
          if (href) { node.href = href; node.target = '_blank'; node.rel = 'noopener noreferrer'; }
          break;
        }
        // Raw HTML is data. textContent keeps scripts, SVG, event attributes,
        // iframes, and HTML-encoded URL tricks out of the live DOM.
        case 'html': case 'escape':
          node = document.createTextNode(token.text || token.raw || '');
          break;
        default:
          node = document.createTextNode(token.text || token.raw || '');
      }
      parent.append(node);
    }
  }

  function render(markdown) {
    const container = element('div', undefined, 'answer-text markdown-body');
    const text = String(markdown ?? '');
    try {
      if (!window.marked?.lexer) throw new Error('Markdown parser unavailable');
      appendTokens(window.marked.lexer(text, { gfm: true, breaks: false }), container);
    } catch {
      container.replaceChildren(element('p', text, 'markdown-fallback'));
    }
    return container;
  }

  window.DatasetMarkdown = Object.freeze({ render });
})();
