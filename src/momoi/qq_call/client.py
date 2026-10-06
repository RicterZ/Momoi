"""Authenticated media-service control, never raw QQ AVSDK calls."""
import asyncio
import aiohttp


async def probe(config):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.request_timeout_seconds)) as session:
            async with session.get(config.bridge_url + '/v1/status', headers={
                'Authorization': 'Bearer ' + config.bridge_token,
            }) as response:
                if response.status == 401:
                    return {'ok': False, 'error': 'Bridge 认证失败'}
                response.raise_for_status()
                value = await response.json()
                if value.get('protocol_version') != 1:
                    return {'ok': False, 'error': 'Bridge 协议版本不匹配'}
                return {'ok': bool(value.get('ready')), 'error': value.get('error') or '',
                        'dependencies': value.get('dependencies', {})}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return {'ok': False, 'error': '无法连接通话 Bridge，请检查地址及服务状态'}
