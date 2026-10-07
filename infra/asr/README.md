# Optional local streaming ASR

Tencent ASR remains available. `sherpa/asr` implements the same `ASRProvider`
contract; QQ calls use its streaming extension when selected. No automatic
switch to Tencent occurs on local service failure.

## Container deployment

```sh
docker compose -f compose.yaml --profile asr build asr
docker compose -f compose.yaml --profile asr up -d --no-deps asr
```

In Dashboard → voice services, select **本地 Sherpa ASR**. For Momoi on the same
Compose network set `endpoint` to `http://asr:8003`; leave `model_path` empty.
For a host process use `http://127.0.0.1:8003`. Only the loopback port is published.
The service has no authentication and must remain on a trusted private network.

The container loads the model once at startup. It owns inference and model files;
Momoi's main container does not install sherpa-onnx or load the model.
`ASR_NUM_THREADS` defaults to 2; `ASR_TRAILING_SILENCE` defaults to 1.2 seconds.
Changing these container settings requires recreating the ASR container. Provider
thread/silence fields apply only to future in-process Windows loading, not to the
remote container.

Model: Chinese streaming Zipformer INT8, 2025-06-30. Archive URL and SHA-256 are
pinned in `model.json`, verified at build time. Runtime does not download models.
One recognizer is shared under a lock; each WebSocket owns an independent stream.
The service limits active requests/streams to four. Disconnect frees stream state.
The application only submits final text to Planner; partial hypotheses are not
sent as owner messages. Tencent keeps its existing WAV segmentation path.

The optional ASR profile does not start automatically. For source deployments,
start it explicitly before selecting the local provider. Cloud Tencent ASR does
not depend on this container.

Example `providers.yaml` binding (keep existing model/TTS bindings):

```yaml
services:
  local_speech:
    adapter: sherpa
bindings:
  asr:
    service: local_speech
    enabled: true
    options:
      endpoint: http://asr:8003
```

Changing the Dashboard ASR provider uses the existing configuration reload flow.
The newly selected provider handles subsequent calls; don't switch it mid-call.

The optional Python `asr` dependency group supports future Windows native loading;
Windows optional installer packaging has not yet been implemented or tested.

## Feasibility measurements — 2026-10-07

All successful inference measurements below ran **inside Linux containers**.
Official sample WAVs from the pinned model archive were used, not QQ recordings.
They are not an accuracy benchmark and do not establish Windows/x86 performance.

| Environment | File decoding RTF | Streaming first partial | File end to last final |
| --- | --- | --- | --- |
| Mac Studio M1 Ultra, OrbStack ARM64, 2 threads, 1.2s endpoint | 0.060–0.068 (16kHz samples) | 0.54–0.84s | 0.99–1.45s |
| Linux ARM64 server, container limited to 1 CPU, 1 thread, 1.2s endpoint | warm approximately 0.072–0.084; first decode 0.121 | approximately 0.53–0.83s | approximately 0.99–1.45s |
| Same Linux server, 0.6s endpoint | CPU remained faster than real time | approximately 0.53–0.83s | 0.33–0.82s |

The 0.6s configuration splits one sample at a natural pause into two final segments;
joined text matches file decoding. File-end timing is **not** a ground-truth
last-speech timestamp and must not be reported as stop-speaking latency/P95.
Only two 16kHz and one 8kHz sample were tested. No corpus CER was measured.

Server HTTP recognition: 5.61s sample returned in approximately 0.48s.
Paced WebSocket test: 431 frames, per-frame request P50 approximately 0.98ms,
P95 approximately 22.8–34.8ms and maximum approximately 59.7–68.4ms across two runs. This is request processing
latency, not total recognition latency. Partial stream cancellation followed by a
fresh session passed; malformed WAV returned HTTP 400.
Idle container memory after test: approximately 292MiB; service process RSS about
330MiB and high-water RSS about 351MiB (different accounting methods).
Cold model load in separate benchmark process: 5.21s initially, 1.96s subsequently.
Warm-up is required before accepting live audio.

Benchmark scripts: `benchmark.py` and `smoke.py`. Run them inside a container with
official test WAVs mounted under `/poc/test_wavs`. The temporary server deployment
is `/tmp/momoi-asr-poc-20261008/compose.yaml`, project/container
`momoi-asr-poc-20261008`, port `127.0.0.1:18003`, 1 CPU / 1GiB limit.
It does not change existing Momoi/NapCat containers or credentials.

Next validation: real QQ recordings, names/mixed-language/noise, longer calls,
endpoint false splits, and whole-call business delivery. Windows optional component
installation and configuration-driven native startup require a separate Windows
implementation/verification pass.

## Business and UI verification

`call_smoke.py` runs in a separate test Momoi container. It replays the official
16kHz WAV through a WebSocket Bridge, the real `QQCallChannel`, and the separate
ASR service. It verifies final owner text, `qq_call` delivery context, playback
interruption, generation increment, and stream cleanup. This does not replace a
real QQ call/accuracy test or exercise LLM/TTS output.

Dashboard UI is audited against the test container with temporary credentials;
local/Tencent choices and responsive layout screenshots are under the ignored
`build/local-asr/ui/` directory. Existing live Momoi is not reconfigured.
