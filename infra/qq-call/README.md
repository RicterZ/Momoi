# Shared QQ call bridge build logic

`bridge_patch.py` applies version-checked additions to the pinned
`RicterZ/maibot-qq-voice-call` plugin. Both client images build from the same
upstream revision and apply the same readiness gate, AV-host diagnostics, and
login state tracking here.

Client-specific orchestration stays in thin adapters because the hosts start
differently: `infra/napcat-call` patches NapCat's launcher and installs its
plugin; `infra/snowluma-call` wraps QQ's original entrypoint and starts services
under SnowLuma's supervisor. The Momoi MediaBroker and `qq_call` integration
remain shared product code.

Build and deployment instructions:

- [NapCat call image](../napcat-call/README.md)
- [SnowLuma call image](../snowluma-call/README.md)

From the repository root, build both derived images without starting them:

```sh
docker compose -f infra/napcat-call/compose.yaml build
docker compose -f docker-compose.yml -f infra/snowluma-call/compose.yaml build snowluma
```

The two adapters are alternatives for a QQ account; do not enable both against
one account. SnowLuma remains experimental pending native amd64 call testing.
