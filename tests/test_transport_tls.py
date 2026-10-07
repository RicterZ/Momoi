import ssl
from unittest.mock import patch

from momoi.integrations.transport import client_tls_context


def test_tls_preserves_os_trust_and_adds_bundled_roots():
    with patch('momoi.integrations.transport.ssl.create_default_context', wraps=ssl.create_default_context) as defaults:
        context = client_tls_context()
        defaults.assert_called_once_with()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.cert_store_stats()['x509_ca'] > 0
