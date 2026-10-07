"""Synchronize the local emotion catalog with QQ's favorite stickers."""
import asyncio
import base64
import hashlib
import logging
from pathlib import Path

from ...channel import ChannelError, SendRejected
from ...observability.events import log_event

logger = logging.getLogger(__name__)


class FavoriteStickers:
    def __init__(self, request, catalog):
        self.request = request
        self.catalog = catalog
        self.changed = asyncio.Event()
        self.changed.set()
        self.lock = asyncio.Lock()
        self.resources = {}

    def notify(self):
        self.changed.set()

    async def _fetch(self):
        response = await self.request('fetch_custom_face_detail', {'count': 1000})
        items = response.get('data')
        if not isinstance(items, list):
            raise SendRejected('NapCat returned invalid favorite sticker details')
        return {str(item.get('md5', '')).lower(): item for item in items
                if isinstance(item, dict) and item.get('md5')}

    async def sync(self):
        async with self.lock:
            catalog = self.catalog()
            if not catalog:
                self.resources = {}
                return
            favorites = await self._fetch()
            resources = {}
            for asset in catalog:
                slug = str(asset['slug'])
                try:
                    path = Path(str(asset['path']))
                    data = await asyncio.to_thread(path.read_bytes)
                    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
                    item = favorites.get(md5)
                    if item is None:
                        uploaded = await self.request('download_file', {
                            'base64': base64.b64encode(data).decode('ascii'),
                            'name': 'momoi-emotion-' + md5 + path.suffix,
                        })
                        remote = (uploaded.get('data') or {}).get('file')
                        if not isinstance(remote, str) or not remote:
                            raise SendRejected('NapCat returned no uploaded sticker path')
                        await self.request('add_custom_face', {'file': remote, 'is_origin': True})
                        # Read the actual QQ resource rather than assume a shape for add's return value.
                        favorites = await self._fetch()
                        item = favorites.get(md5)
                    if not item or not item.get('url'):
                        raise SendRejected('QQ favorite sticker is not yet available')
                    resources[slug] = {'md5': md5, 'url': str(item['url'])}
                except (OSError, ChannelError) as error:
                    log_event(logger, logging.WARNING, 'qq_favorite_sync_failure',
                              channel='napcat', slug=slug, error_type=type(error).__name__,
                              reason=str(error))
            self.resources = resources
            log_event(logger, logging.INFO, 'qq_favorite_sync_complete', channel='napcat',
                      synced=len(resources), total=len(catalog))

    async def run(self, ready):
        while True:
            await self.changed.wait()
            await ready.wait()
            self.changed.clear()
            try:
                await self.sync()
            except ChannelError as error:
                log_event(logger, logging.WARNING, 'qq_favorite_sync_failure', channel='napcat',
                          error_type=type(error).__name__, reason=str(error))

    async def segment(self, slug):
        if self.changed.is_set() or slug not in self.resources:
            await self.sync()
        resource = self.resources.get(slug)
        if resource is None:
            raise SendRejected('QQ favorite sticker is not synchronized: ' + slug)
        return {'type': 'image', 'data': {'file': resource['url'], 'sub_type': 1}}
