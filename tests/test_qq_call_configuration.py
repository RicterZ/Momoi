import pytest
from momoi.channel.napcat.config import NapCatConfig, QQCallConfig
from momoi.config.manager import redact, restore_secrets
from momoi.integrations.registry import adapter_definition


def test_call_settings_default_and_secret_roundtrip():
    config = NapCatConfig.from_mapping({'url': 'ws://localhost', 'owner_qq': '123456'})
    assert not config.voice_call.enabled
    value = {'enabled': True, 'bridge_url': 'http://localhost:6112', 'bridge_token': 'a' * 64}
    parsed = QQCallConfig.from_mapping(value)
    assert parsed.enabled and 'a' * 64 not in repr(parsed)
    assert redact(value)['bridge_token'] == {'$secret': 'keep'}
    assert restore_secrets(redact(value), value) == value


@pytest.mark.parametrize('value', [
    {'enabled': 'false'}, {'enabled': True},
    {'bridge_url': 'ftp://localhost'}, {'bridge_url': 'http://user:pass@localhost'},
    {'request_timeout_seconds': 0}, {'request_timeout_seconds': True}, {'surprise': True},
])
def test_invalid_call_settings(value):
    with pytest.raises(ValueError):
        QQCallConfig.from_mapping(value)


def test_asr_registered_and_defaults():
    adapter = adapter_definition('tencent', 'asr')
    assert adapter.schema['engine']['default'] == '16k_zh'
    assert adapter.schema['secret_key']['secret']
    assert adapter.test is not None


def test_phone_requires_its_own_token_and_preserves_both_secrets():
    token = 'connection-secret-' * 3
    channel = {'url': 'ws://localhost', 'owner_qq': '123456', 'access_token': token,
               'voice_call': {'enabled': True, 'bridge_url': 'http://localhost:6112', 'bridge_token': ''}}
    with pytest.raises(ValueError):
        NapCatConfig.from_mapping(channel)
    channel['voice_call']['bridge_token'] = token
    masked = redact(channel)
    assert masked['access_token'] == {'$secret': 'keep'}
    assert masked['voice_call']['bridge_token'] == {'$secret': 'keep'}
    assert restore_secrets(masked, channel) == channel
    independent = 'independent-secret-' * 3
    channel['voice_call']['bridge_token'] = independent
    assert NapCatConfig.from_mapping(channel).voice_call.bridge_token == independent
