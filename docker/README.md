# Momoi

A persistent personal AI companion for private chat. Image: `ricterz/momoi`.

Architectures: `linux/amd64`, `linux/arm64`.

## One command

No provider keys, channels, or existing configuration files are required.

```bash
docker run -d --name momoi --restart unless-stopped \
  -e TZ=Asia/Shanghai -e MOMOI_TIMEZONE=Asia/Shanghai \
  -v "$HOME/.momoi:/home/momoi/.momoi" \
  -p 8788:8788 \
  ricterz/momoi:latest
```

On an empty volume, `momoi run` creates a minimal workspace and prints the generated
dashboard passphrase in `docker logs momoi`. Existing files are preserved. Open
`http://127.0.0.1:8788`, sign in, then configure models, channels and optional
capabilities in Settings. Weixin QR login is available in the dashboard.
Pin a version tag from the Tags tab if you do not want `latest`.

The published Compose file starts only Momoi. Start optional services as needed:

```bash
docker compose -f docker-compose.yml --profile embedding up -d
```

Then configure `http://embedding:8002/v1/embeddings` in Settings and enable semantic
recall. For QQ, deploy NapCat separately and configure its reachable OneBot WebSocket
URL in Settings. Remote embedding providers require no local encoder container.
For source builds, use `docker compose -f compose.yaml up -d --build`.
Specify `-f` explicitly: Compose otherwise prefers `compose.yaml` over `docker-compose.yml`.

## Environment

| Variable | Purpose |
| --- | --- |
| `MOMOI_TIMEZONE` | Application timezone; set alongside container `TZ` |
| `MOMOI_DASHBOARD_TOKEN` | Dashboard passphrase. Generated on first start if omitted |
| `MOMOI_WORKSPACE` | Compose host workspace directory, default `~/.momoi`; container mount remains `/home/momoi/.momoi` |
| `MOMOI_DASHBOARD_PORT` | Compose host dashboard port, default `8788` |

Configure provider keys and channels in Settings. Webhooks are disabled by default;
add a port mapping for 8787 and configure the listener and authentication when
enabling them. Use `--workspace /path run` to override the workspace inside a
container, or `run --no-dashboard` for an already configured headless deployment.

Source and compose file: https://github.com/RicterZ/Momoi
