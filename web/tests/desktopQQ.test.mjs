import test from 'node:test';
import assert from 'node:assert/strict';
import { desktopQQ, hasDesktopQQ } from '../src/desktopQQ.js';

test('desktop QQ bridge rejects browser use and isolates concurrent replies', async () => {
  assert.equal(hasDesktopQQ({}), false);
  await assert.rejects(desktopQQ('start', {}, {}), /Windows/);
  const listeners = new Set();
  const sent = [];
  const bridge = {
    addEventListener: (_, handler) => listeners.add(handler),
    removeEventListener: (_, handler) => listeners.delete(handler),
    postMessage: message => sent.push(message),
  };
  const target = { chrome: { webview: bridge } };
  const first = desktopQQ('status', {}, target);
  const second = desktopQQ('start', { bot_qq: '12345', action: 'stop', type: 'other' }, target);
  assert.equal(sent[1].action, 'start');
  assert.equal(sent[1].type, 'momoi-qq');
  for (const handler of listeners) handler({ data: { ...sent[0], id: 'unrelated', result: 'bad' } });
  assert.equal(listeners.size, 2);
  for (const handler of [...listeners]) handler({ data: { ...sent[1], error: '端口已占用' } });
  await assert.rejects(second, /端口/);
  for (const handler of [...listeners]) handler({ data: { ...sent[0], result: { running: true } } });
  assert.deepEqual(await first, { running: true });
  assert.equal(listeners.size, 0);
});

test('failed native post does not leak a response listener', async () => {
  const listeners = new Set();
  const bridge = {
    addEventListener: (_, handler) => listeners.add(handler),
    removeEventListener: (_, handler) => listeners.delete(handler),
    postMessage: () => { throw new Error('closed'); },
  };
  await assert.rejects(desktopQQ('status', {}, { chrome: { webview: bridge } }), /closed/);
  assert.equal(listeners.size, 0);
});
