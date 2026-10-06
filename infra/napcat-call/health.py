import os
import urllib.request
if os.getenv('QQ_CALL_ENABLED', '0') == '1':
    with urllib.request.urlopen('http://127.0.0.1:' + os.getenv('QQ_CALL_PORT', '6112') + '/healthz', timeout=3) as response:
        assert response.status == 200
