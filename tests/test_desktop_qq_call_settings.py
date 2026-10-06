"""Desktop-managed connection values cannot be overridden by dashboard input."""
import json

import pytest

from momoi.config.manager import managed_qq_call
from momoi.config.models import ConfigError


def test_desktop_connection_overrides_user_values_preserving_preferences(tmp_path, monkeypatch):
    path = tmp_path / 'managed.json'
    path.write_text(json.dumps({'bridge_url': 'http://127.0.0.1:43210', 'bridge_token': 'a' * 64}))
    monkeypatch.setenv('MOMOI_QQ_CALL_MANAGED', str(path))
    app = {'channels': {'enabled': {'napcat': {'voice_call': {
        'enabled': True, 'request_timeout_seconds': 12,
        'bridge_url': 'http://other:6112', 'bridge_token': 'user-value',
    }}}}}
    result = managed_qq_call(app)['channels']['enabled']['napcat']['voice_call']
    assert result == {'enabled': True, 'request_timeout_seconds': 12,
                      'bridge_url': 'http://127.0.0.1:43210', 'bridge_token': 'a' * 64}


def test_non_desktop_keeps_external_bridge(monkeypatch):
    monkeypatch.delenv('MOMOI_QQ_CALL_MANAGED', raising=False)
    app = {'channels': {'enabled': {'napcat': {'voice_call': {'bridge_url': 'http://linux:6112'}}}}}
    assert managed_qq_call(app) == app


@pytest.mark.parametrize('url,token', [('http://example.com:6112', 'a' * 64),
                                     ('http://127.0.0.1:6112', 'short')])
def test_invalid_shell_metadata_fails_closed(tmp_path, monkeypatch, url, token):
    path = tmp_path / 'managed.json'
    path.write_text(json.dumps({'bridge_url': url, 'bridge_token': token}))
    monkeypatch.setenv('MOMOI_QQ_CALL_MANAGED', str(path))
    with pytest.raises(ConfigError, match='metadata is invalid'):
        managed_qq_call({})
