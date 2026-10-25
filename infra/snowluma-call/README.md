# SnowLuma voice-call bridge (experimental)

This opt-in Linux image connects Momoi's existing `qq_call`, MediaBroker,
ASR/TTS and half-duplex pipeline to the QQ client managed by SnowLuma v2.2.0.
It does **not** load NapCat, install a NapCat plugin, replace SnowLuma's native
hook, or create a second logged-in QQ session. Windows packaging is unchanged.

QQ's main entry loads a small observer before its original entry. The observer
captures the session returned by QQ's own `NodeIQQNTWrapperSession.create()` /
`getNTWrapperSession()` and its `init` identity. Once ready, the existing bridge
registers `getAVSDKService().addKernelAVSDKListener` and sends kernel replies
through `setActionFromAVSDK`. A separate QQ/Electron **AV host only** loads the
bundled `libAVSDKPlugin.so`; PulseAudio carries audio. The existing authenticated
loopback HTTP protocol remains unchanged.

## Start

From the repository root, use the release Compose file plus the overlay:

```sh
docker compose -f docker-compose.yml -f infra/snowluma-call/compose.yaml build snowluma
docker compose -f docker-compose.yml -f infra/snowluma-call/compose.yaml up -d --build
```

To build Momoi locally too, replace `docker-compose.yml` with `compose.yaml`.
The build verifies the pinned bridge archive's SHA256 and patches QQ's main
entry in the derived image only. Existing QQ login data and SnowLuma config
remain in their existing volumes. Keep one QQ account per container; extra QQ
homes are rejected when calling is enabled. Do not enable this alongside another
call bridge for the same account. `QQ_CALL_RUNTIME` must remain under `/app/data`
so SnowLuma's startup applies the configured UID/GID to its private state.

1. Log into QQ through the existing noVNC page; configure OneBot as usual.
2. Read `/app/data/qq-call/runtime/control.token` inside the SnowLuma container
   privately (for example, `docker compose exec snowluma cat` followed by that
   path). Paste it into Momoi's QQ voice-call token setting. It is a separate
   secret from the OneBot token, created once with mode `0600`, never logged.
3. Set the voice-call bridge URL to `http://snowluma:6112` when Momoi is on the
   same Compose network, or `http://127.0.0.1:6112` for a local host process.
   Only the media broker's authenticated API is published, bound to host loopback;
   native control/AV host ports 6110/6111 remain container-loopback-only.
4. Configure the owner QQ, ASR and TTS, then enable voice calls in Momoi. Calls
   are answered only while Momoi continually reports readiness for that owner.

Supervisor owns the audio server, AV host and broker; each is restarted if it
exits, and stops with the container. State lives in the existing `snowluma-data`
volume. The token survives restarts. To roll back, recreate SnowLuma using only
the original Compose file; no persistent QQ launcher was modified.

## Verification and current limitation

Session capture and validation have isolated Node tests; runtime initialization,
entrypoint preparation, existing media behavior and Compose structure have local
checks. These checks are **not evidence of a successful QQ call**.

The local Apple Silicon environment currently fails SnowLuma's native hook
injection with `COMPONENT_LOAD_FAILED`. Native **amd64** verification is still
required before treating this migration as production-ready:

- Confirm OneBot and the normal QQ GUI remain connected after login and restart.
- Confirm Momoi reports the listener, AV-host login and audio devices ready.
- Call from the owner: ringing → connected, ASR input, audible TTS and half-duplex.
- Confirm a non-owner call and an owner call while Momoi is disconnected are not
  automatically accepted. Test hangup, QQ logout/relogin and container restart.
- Verify no extra logged-in account/session is created by the AV host.

If QQ changes its native session API, the observer reports an unavailable bridge
rather than creating a session or guessing identities. Do not override this
failure with a hardcoded QQ identity. The original messaging-only image remains
the default until real-client verification is complete.

The bridge is pinned to `RicterZ/maibot-qq-voice-call` revision
`22f30c021cd3170f75af9dff66cc959a07ebda4b`; its GPL license and third-party notices
are retained under `/opt/qq-call/upstream`. SnowLuma's own licensing still applies
to its base image; this integration does not modify its runtime bundle.

The upstream bridge patch itself lives in shared `infra/qq-call/bridge_patch.py`;
the NapCat and SnowLuma `prepare.py` files apply it to their own derived image.
Build/run details for the NapCat adapter are in
[`infra/napcat-call/README.md`](../napcat-call/README.md).
