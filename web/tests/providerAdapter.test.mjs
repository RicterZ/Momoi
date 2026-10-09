import assert from 'node:assert/strict';
import test from 'node:test';
import { switchProviderAdapter } from '../src/providerAdapter.js';

const adapters = [
  { capability: 'tts', adapter: 'fish', fields: {
    base_url: { default: 'https://api.fish.audio' }, api_key: {},
    reference_id: {}, format: { default: 'mp3' }, timeout_seconds: { default: 60 },
  } },
  { capability: 'tts', adapter: 'vocu', fields: {
    base_url: { default: 'https://v1.wusound.cn/api' }, api_key: {}, voice_id: {},
    preset: { default: 'balance', enum: ['balance', 'stable'] }, timeout_seconds: { default: 60 },
  } },
];

test('switching TTS replaces endpoint and removes unsupported options', () => {
  for (const base_url of ['https://api.fish.audio', 'https://fish-proxy.example']) {
    const value = { adapter: 'fish', enabled: true, options: {
      base_url, api_key: 'synthetic', reference_id: 'old-voice', format: 'mp3', timeout_seconds: 90,
    } };
    const result = switchProviderAdapter('tts', value, 'vocu', adapters);
    assert.deepEqual(result.options, { base_url: 'https://v1.wusound.cn/api', api_key: 'synthetic',
      preset: 'balance', timeout_seconds: 90 });
    assert.equal(value.options.base_url, base_url);
    assert.equal(result.enabled, true);
  }
});

test('same adapter preserves manually configured options', () => {
  const value = { adapter: 'vocu', options: { base_url: 'https://custom.example' } };
  assert.equal(switchProviderAdapter('tts', value, 'vocu', adapters), value);
});

test('LLM protocol switch without supplier defaults retains relay endpoint', () => {
  const protocols = ['openai', 'anthropic'].map(adapter => ({
    capability: 'llm', adapter, fields: { base_url: {}, api_key: {}, model: {} },
  }));
  const value = { adapter: 'openai', options: { base_url: 'https://relay.example', api_key: 'key', model: 'model' } };
  assert.deepEqual(switchProviderAdapter('llm', value, 'anthropic', protocols).options, value.options);
});

test('ASR switches between Tencent credentials and local model without leaking options', () => {
  const adapters = [
    { capability: 'asr', adapter: 'tencent', fields: {
      secret_id: { secret: true }, secret_key: { secret: true }, timeout_seconds: { default: 30 },
    } },
    { capability: 'asr', adapter: 'sherpa', fields: {
      model_path: { default: '' },
    } },
  ];
  const local = switchProviderAdapter('asr', {
    adapter: 'tencent', enabled: true, options: { secret_id: 'test-id', secret_key: 'test-key' },
  }, 'sherpa', adapters);
  assert.equal(local.adapter, 'sherpa');
  assert.equal(local.enabled, true);
  assert.equal(local.options.secret_id, undefined);
  assert.equal(local.options.secret_key, undefined);
  local.options.model_path = '/models/asr';
  const cloud = switchProviderAdapter('asr', local, 'tencent', adapters);
  assert.equal(cloud.options.endpoint, undefined);
  assert.equal(cloud.options.model_path, undefined);
});

test('LLM protocol switch preserves an explicit relay despite different defaults', () => {
  const protocols = [
    { capability: 'llm', adapter: 'openai', fields: { base_url: { default: 'https://api.deepseek.com/v1' }, model: { default: 'deepseek-flash' } } },
    { capability: 'llm', adapter: 'anthropic', fields: { base_url: {}, model: {} } },
  ];
  for (const adapter of ['openai', 'anthropic']) {
    const value = { adapter, options: { base_url: 'https://relay.example', model: 'custom-model' } };
    assert.deepEqual(switchProviderAdapter('llm', value, adapter === 'openai' ? 'anthropic' : 'openai', protocols).options, value.options);
  }
});
