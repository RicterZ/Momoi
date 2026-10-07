"""Use Windows system proxies for external services, never loopback components."""
import os
from urllib.request import getproxies


def prepare_proxy_environment():
    proxies = getproxies()
    for scheme in ('http', 'https'):
        value = proxies.get(scheme)
        if value:
            os.environ.setdefault(scheme.upper() + '_PROXY', value)
    existing = os.environ.get('no_proxy', os.environ.get('NO_PROXY', ''))
    bypass = [entry.strip() for entry in existing.split(',') if entry.strip()]
    for host in ('localhost', '127.0.0.1', '::1'):
        if host not in bypass:
            bypass.append(host)
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = ','.join(bypass)
