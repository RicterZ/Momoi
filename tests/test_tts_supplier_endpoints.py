from pathlib import Path

import pytest

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.integrations.configuration import parse_provider_catalog
from momoi.integrations.registry import ServiceRegistry, adapter_definition


@pytest.mark.parametrize('adapter,endpoint,voice', [
    ('fish', 'https://api.fish.audio', {'reference_id': 'synthetic'}),
    ('vocu', 'https://v1.wusound.cn/api', {'voice_id': 'synthetic'}),
])
@pytest.mark.parametrize('location', ['service', 'settings', 'binding'])
def test_obsolete_saved_url_cannot_redirect_tts(adapter, endpoint, voice, location):
    service = {'adapter': adapter, 'settings': {'api_key': 'synthetic', **voice}}
    binding = {'service': 'speech', 'enabled': True}
    obsolete = 'https://wrong-supplier.example/api'
    if location == 'service':
        service['base_url'] = obsolete
    elif location == 'settings':
        service['settings']['base_url'] = obsolete
    else:
        binding['options'] = {'base_url': obsolete}
    catalog = parse_provider_catalog({'version': 1, 'services': {'speech': service},
        'bindings': {'tts': binding}}, Path('providers.yaml'))
    assert 'base_url' not in catalog.options_for('tts')
    assert 'base_url' not in adapter_definition(adapter, 'tts').schema
    assert ServiceRegistry(catalog).tts.base_url == endpoint


def test_backend_switch_drops_stale_browser_endpoint(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    for adapter, endpoint, voice in [
        ('fish', 'https://api.fish.audio', {'reference_id': 'synthetic'}),
        ('vocu', 'https://v1.wusound.cn/api', {'voice_id': 'synthetic'}),
        ('fish', 'https://api.fish.audio', {'reference_id': 'synthetic'}),
    ]:
        snapshot = manager.save_binding('tts', {'adapter': adapter, 'enabled': True,
            'options': {'api_key': 'synthetic', 'base_url': 'https://previous-supplier.example', **voice}},
            manager.revision())
        assert 'base_url' not in snapshot['capabilities']['tts']['options']
        assert 'base_url' not in snapshot['providers']['services']['configured_tts']['settings']
        assert ServiceRegistry(manager.validate().providers).tts.base_url == endpoint
