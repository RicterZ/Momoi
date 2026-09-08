import test from 'node:test';
import assert from 'node:assert/strict';
import { waitForConfiguration } from '../src/configurationRuntime.js';

const current = { saved_revision: 'new', observed_revision: 'new', applied_revision: 'new', state: 'running', runtime_active: true };
const options = { timeout: 100, interval: 1 };

test('waits for the saved revision to be active, ignoring an old running process and an old failure', async () => {
  const statuses = [
    { ...current, applied_revision: 'old' },
    { ...current, observed_revision: 'old', applied_revision: 'old', state: 'error' },
    { ...current, state: 'applying', runtime_active: false },
    { ...current, runtime_active: false },
    current,
  ];
  let reads = 0;
  const result = await waitForConfiguration(async () => statuses[reads++], 'new', options);
  assert.equal(result.state, 'success');
  assert.equal(reads, 5);
});

test('reports a failed revision even if the previous process was restored', async () => {
  const result = await waitForConfiguration(async () => ({ ...current, state: 'error', applied_revision: 'old', error: 'startup failed; previous configuration restored' }), 'new', options);
  assert.equal(result.state, 'error');
  assert.match(result.message, /previous configuration restored/);
});

test('saved setup completes without inspecting missing configuration', async () => {
  const result = await waitForConfiguration(async () => ({ ...current, state: 'setup', runtime_active: false, get missing() { throw new Error('Missing configuration must not be inspected'); } }), 'new', options);
  assert.equal(result.state, 'success');
  assert.equal(result.message, '');
});

test('recovers from a transient status request failure', async () => {
  let reads = 0;
  const result = await waitForConfiguration(async () => { if (++reads === 1) throw new Error('offline'); return current; }, 'new', options);
  assert.equal(result.state, 'success');
  assert.equal(reads, 2);
});

test('reports timeout instead of success when status remains unavailable', async () => {
  const result = await waitForConfiguration(async () => { throw new Error('offline'); }, 'new', { timeout: 10, interval: 1 });
  assert.equal(result.state, 'timeout');
  assert.match(result.message, /offline/);
});

test('does not attribute another saved revision to this operation', async () => {
  const result = await waitForConfiguration(async () => ({ ...current, saved_revision: 'other' }), 'new', options);
  assert.equal(result.state, 'error');
  assert.match(result.title, /被更新/);
});

test('cancels monitoring on unmount', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(waitForConfiguration(async () => current, 'new', { ...options, signal: controller.signal }), { name: 'AbortError' });
});
