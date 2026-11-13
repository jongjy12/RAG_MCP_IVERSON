// Focused renderer contract checks without browser or provider dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const marked = require('../static/vendor/marked/marked.umd.js');

class Node {
  constructor(tag, text = '') { this.tagName=tag; this.children=[]; this.attributes={}; this.textContent=text; }
  set textContent(text) { this.children=[]; this.text=String(text); }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text=''; this.children=nodes; }
  setAttribute(name, value) { this.attributes[name]=value; }
}
const document = { createElement: tag => new Node(tag), createTextNode: text => new Node('#text', text) };
const context = { window:{marked}, document, URL };
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/markdown.js'),'utf8'),context);
const render = context.window.DatasetMarkdown.render;
const descendants = (node, tag) => [node,...node.children.flatMap(child => descendants(child))].filter(child => !tag || child.tagName===tag);

const answer = render('## Survival rates\n\n**Female** passengers: *higher* survival.\n\n| Sex | Rate |\n|---|---:|\n| Female | **74.20%** |\n| Male | 18.89% |\n\n- First\n- Second\n\n> Missing ages excluded.\n\n```python\nprint("<safe>")\n```');
assert.equal(descendants(answer,'h2')[0].textContent,'Survival rates');
assert.equal(descendants(answer,'strong').length,2);
assert.equal(descendants(answer,'table').length,1);
assert.equal(descendants(answer,'tr').length,3);
assert.equal(descendants(answer,'td')[1].className,'align-right');
assert.equal(descendants(answer,'li').length,2);
assert.equal(descendants(answer,'blockquote').length,1);
assert.equal(descendants(answer,'pre')[0].textContent,'print("<safe>")');
assert.equal(render('A &amp; B &lt; 2 and &#8805; 0').textContent,'A & B < 2 and ≥ 0');
assert.equal(descendants(render('`&amp;`'),'code')[0].textContent,'&amp;');

const unsafe = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert%281%29)\n\n[encoded](jav&#x61;script:alert%281%29)\n\n![tracking](https://example.com/tracking.png)\n\n[Source](https://example.com/?a=1&amp;b=2)');
for (const tag of ['script','img','iframe','svg','style']) assert.equal(descendants(unsafe,tag).length,0);
for (const link of descendants(unsafe,'a')) {
  assert.match(link.href,/^(https?:|mailto:)/);
  assert.equal(link.rel,'noopener noreferrer');
  assert.equal(link.target,'_blank');
}
assert(descendants(unsafe,'a').some(link => link.href==='https://example.com/?a=1&b=2'));
const tasks=render('- [x] Prepared\n- [ ] Pending\n\n~~old value~~\n\n3. Third\n4. Fourth');
assert.equal(descendants(tasks,'input').length,2);
assert(descendants(tasks,'input').every(input => input.disabled));
assert.equal(descendants(tasks,'del').length,1);
assert.equal(descendants(tasks,'ol')[0].start,3);
console.log('Markdown checks passed: formatting, tables, code, entities, tasks, safe links, and inert HTML/images.');
