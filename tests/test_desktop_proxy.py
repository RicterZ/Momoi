from momoi_desktop import proxy


def test_system_proxy_and_loopback_bypass(monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(proxy, 'getproxies', lambda: {'http': 'http://localhost:7890', 'https': 'http://localhost:7890'})
    proxy.prepare_proxy_environment()
    assert proxy.os.environ['HTTPS_PROXY'] == 'http://localhost:7890'
    assert set(proxy.os.environ['NO_PROXY'].split(',')) == {'localhost', '127.0.0.1', '::1'}


def test_preserves_explicit_proxy_and_bypass(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://custom:8080')
    monkeypatch.setenv('no_proxy', 'internal.example')
    monkeypatch.setattr(proxy, 'getproxies', lambda: {'https': 'http://system:7890'})
    proxy.prepare_proxy_environment()
    assert proxy.os.environ['HTTPS_PROXY'] == 'http://custom:8080'
    assert 'internal.example' in proxy.os.environ['NO_PROXY']
