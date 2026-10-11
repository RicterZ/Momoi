import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runtimeFieldChanges } from './runtimeFields.js';

test('numeric inputs produce numbers and preserve unchanged values', () => {
  const spec = { type: 'number', label: '日预算（元）' };
  assert.equal(runtimeFieldChanges(spec, '2.5', 0), 2.5);
  assert.equal(runtimeFieldChanges(spec, ' 2.5 ', 2.5), undefined);
  assert.equal(runtimeFieldChanges(spec, 0, 2), 0);
  for (const value of ['', 'abc', 'Infinity', NaN]) {
    assert.throws(() => runtimeFieldChanges(spec, value, 0), /有效数字/);
  }
  assert.throws(() => runtimeFieldChanges({ type: 'integer' }, '1.2', 0), /整数/);
});
