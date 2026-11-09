import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { stripTypeScriptTypes } from 'node:module';

const listeners = {};
const output = { textContent: '' };
let metadata = true;
let clipboard;
const toolbar = {
  hidden: true,
  querySelector: selector => selector === '[data-export-metadata]' ? { checked: metadata } : output,
  addEventListener: (name, fn) => { listeners.export = fn; },
};
class MessageTarget {
  constructor(id, body, timestamp, author) {
    this.interactive = false;
    this.message = {
      dataset: { messageId: id, author, sentAt: timestamp },
      querySelector: () => ({ dataset: { exportBody: body }, querySelectorAll: () => [] }),
    };
  }
  matches() { return true; }
  closest(selector) { return selector.startsWith('.message') ? this.message : this.interactive ? {} : null; }
}
const context = vm.createContext({
  document: {
    querySelector: selector => selector === '.export-toolbar' ? toolbar : {},
    querySelectorAll: () => [],
    createTreeWalker: () => ({ nextNode: () => false }),
    createRange: () => ({}),
    addEventListener: (name, fn) => { listeners[name] = fn; },
  },
  Element: MessageTarget,
  HTMLElement: MessageTarget,
  window: { getSelection: () => ({ isCollapsed: true }) },
  NodeFilter: { SHOW_TEXT: 4 },
  MutationObserver: class { observe() {} },
  navigator: { clipboard: { writeText: async text => { clipboard = text; }, write: async items => { clipboard = items; } } },
  ClipboardItem: class { constructor(parts) { this.parts = parts; } },
  Blob, URL, setTimeout,
});
const source = stripTypeScriptTypes(readFileSync('frontend/export.ts', 'utf8')).replace('export {};', '');
vm.runInContext(source, context);
const select = target => listeners.click({ target, button: 0, detail: 1 });
const click = action => listeners.export({ target: { closest: () => ({ matches: () => false, dataset: { export: action } }) } });
select(new MessageTarget('2', 'Later\nmessage', '2026-01-02T00:00:00Z', 'Bob'));
select(new MessageTarget('1', '<script>alert(1)</script>', '2026-01-01T00:00:00Z', 'Alice *admin*'));
assert.equal(toolbar.hidden, false);
await click('markdown');
assert.ok(clipboard.indexOf('Alice') < clipboard.indexOf('Bob'));
assert.ok(clipboard.replaceAll('\\', '').includes('2026-01-01T00:00:00Z'));
assert.ok(clipboard.includes('Alice \\*admin\\*'));
metadata = false;
await click('rich');
const html = await clipboard[0].parts['text/html'].text();
assert.ok(html.includes('&lt;script&gt;'));
assert.ok(!html.includes('Alice'));
assert.ok((await clipboard[0].parts['text/plain'].text()).includes('Later\nmessage'));
context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
await click('markdown');
assert.ok(output.textContent.includes('Clipboard unavailable'));
const ignored = new MessageTarget('3', 'Ignored', '2026-01-03T00:00:00Z', 'Carol');
ignored.interactive = true;
select(ignored);
context.window.getSelection = () => ({ isCollapsed: false });
ignored.interactive = false;
select(ignored);
context.navigator.clipboard.writeText = async text => { clipboard = text; };
await click('markdown');
assert.ok(!clipboard.includes('Ignored'));
context.window.getSelection = () => ({ isCollapsed: true });
select(ignored);
await click('markdown');
assert.ok(clipboard.includes('Ignored'));
select(ignored);
await click('markdown');
assert.ok(!clipboard.includes('Ignored'));
console.log('Free-space selection, deselection, interactive controls, text selection, chronological export, metadata toggle, HTML escaping, rich/plain clipboard, and denied clipboard checks passed.');
