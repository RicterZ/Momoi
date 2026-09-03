# Momoi

A persistent personal AI companion for private chat. Image: `ricterz/momoi`.

Architectures: `linux/amd64`, `linux/arm64`.

## One command

NapCat must already be running and exposing OneBot WebSocket on port `3001`.

```bash
docker run -d --name momoi --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -e TZ=Asia/Shanghai \
  -e MOMOI_OWNER_QQ=your-qq-number \
  -v "$HOME/.momoi:/home/momoi/.momoi" \
  -p 8787:8787 -p 8788:8788 \
  ricterz/momoi:latest
```

The first start copies a workspace into the volume, turns on webhooks at `0.0.0.0:8787`, and prints the dashboard and webhook tokens in `docker logs momoi`. Open `http://127.0.0.1:8788` and configure the model connection under Settings. Pin a version tag from the Tags tab if you do not want `latest`.

For Weixin instead of QQ, omit `MOMOI_OWNER_QQ` and run:

```bash
docker run --rm -it \
  -v "$HOME/.momoi:/home/momoi/.momoi" \
  ricterz/momoi:latest channel login weixin
```

Then set `MOMOI_PRIMARY=weixin` on the next start.

## Environment

| Variable | Purpose |
| --- | --- |
| `MOMOI_OWNER_QQ` | Owner QQ accepted by NapCat |
| `MOMOI_NAPCAT_URL` | OneBot WebSocket URL. Default for `docker run`: `ws://host.docker.internal:3001` |
| `MOMOI_PRIMARY` | `napcat` or `weixin` |
| `MOMOI_TIMEZONE` | Notification timezone. Falls back to `TZ` |
| `MOMOI_DASHBOARD_TOKEN` | Dashboard passphrase. Generated on first start if omitted |
| `MOMOI_WEBHOOKS_ENABLED` | Default `true` on first start |
| `MOMOI_WEBHOOKS_TOKEN` | Webhook bearer token. Generated on first start if omitted |
| `MOMOI_USAGE_API_KEY` | Optional Usage plugin key |

Source and compose file: https://github.com/RicterZ/Momoi
