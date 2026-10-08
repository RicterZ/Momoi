# ASR verification tools

Manual checks for the local Sherpa ASR provider. These tools require model files
and test audio supplied by the developer; neither is committed to this directory.
Run from the repository root, or mount the tools into a container with Momoi and
the required dependencies installed.

## Model benchmark

Requires `sherpa-onnx` and the prepared model directory:

```sh
uv run --locked --group test --extra asr python tools/asr/benchmark.py \
  --model /path/to/models --threads 2 --trailing-silence 0.8 --paced /path/to/sample.wav
```

Reports file decoding time and real-time streaming endpoints. File-end timing
is not a measurement of the last spoken phoneme.

## Provider smoke check

Requires a running private ASR service and a 16 kHz, mono, 16-bit WAV:

```sh
uv run --locked --group test python tools/asr/smoke.py /path/to/sample.wav \
  --endpoint http://127.0.0.1:8003 --trailing-silence 0.8
```

Checks HTTP recognition, WebSocket recognition, stream isolation, and invalid
WAV rejection.

## QQ call integration check

Use an isolated test workspace configured for Sherpa ASR:

```sh
uv run --locked --group test python tools/asr/call_smoke.py /path/to/sample.wav \
  --config /path/to/test-workspace/config.json --expected-text 'Expected transcript'
```

Replays audio through a temporary bridge on a dynamic port and checks event
routing, interruption, and stream cleanup. Use a short sample that produces one
final segment; the check expects one event. It does not place a real QQ call.
