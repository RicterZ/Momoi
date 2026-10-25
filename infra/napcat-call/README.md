# NapCat Voice Call Bridge

A Linux Docker image that adds QQ voice calls to NapCat for Momoi. It combines NapCat, the QQ AVSDK bridge, and an audio broker while retaining NapCat's existing messaging support.

## Features

- Automatically answers incoming calls from the configured owner when Momoi is ready.
- Routes incoming speech through Momoi's ASR, Planner, and Replyer, then plays synthesized replies back into the call.
- Shares conversation history with regular QQ messages.
- Streams reply audio and prefetches subsequent speech bubbles for sequential playback.
- Interrupts playback when the owner speaks and cancels pending audio when the call ends.
- Supports running Momoi and the bridge on separate hosts.

## Components

- **NapCat:** QQ messaging and call events.
- **AVSDK bridge:** Native QQ call handling and audio routing, based on `RicterZ/maibot-qq-voice-call`.
- **Audio broker:** Authenticated connection to Momoi for incoming audio, playback, and call status.

Momoi supplies speech recognition, reply generation, and speech synthesis. This image contains the Linux call infrastructure; it does not provide standalone conversational behavior.

## Build and run

From the repository root:

```sh
docker compose -f infra/napcat-call/compose.yaml build
QQ_CALL_ENABLED=1 docker compose -f infra/napcat-call/compose.yaml up -d
```

The build uses the pinned NapCat image and the SHA256-verified upstream AVSDK
bridge. The Compose file publishes NapCat WebUI/OneBot and the authenticated
call broker on loopback. Persistent QQ login/config and call state use the
`NAPCAT_QQ_DATA`, `NAPCAT_CONFIG`, and `QQ_CALL_DATA` bind mounts (defaults are
under `./data`). Set Momoi's voice-call bridge URL to an address that can reach
port `6112`, and use the token in the configured `QQ_CALL_DATA` directory at
`runtime/control.token`; the call token is separate from the OneBot token.

To disable calling while keeping NapCat messaging, recreate with
`QQ_CALL_ENABLED=0`. To remove the call service, stop this Compose project; it
does not affect the separate SnowLuma service.

The pinned bridge patch/readiness handling is shared at
[`infra/qq-call/bridge_patch.py`](../qq-call/bridge_patch.py). SnowLuma's
separate image build and opt-in deployment instructions are in
[`infra/snowluma-call/README.md`](../snowluma-call/README.md).
