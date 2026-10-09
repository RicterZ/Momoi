# Bundled models

Run these commands from the repository root before building a Linux image:

```bash
uv run --locked python packaging/prepare_embedding.py
uv run --locked python packaging/prepare_asr.py
```

They prepare `models/bge-small-zh-v1.5/` and `models/asr/` from pinned upstream
artifacts. ASR archives are checked against `packaging/asr-model.json`; BGE uses
a pinned revision and verifies offline inference. Downloaded weights are ignored
by Git and remain outside `src/` and the Python wheel.

Both Dockerfiles copy these directories to `/opt/momoi/models/`. The Docker
release workflow prepares them before building. Windows builds use the same root
directories, copying BGE into the core installer and ASR into its optional component.
Models are never downloaded during recognition or embedding.

For source execution, run Momoi from the repository root. Local ASR also needs
`uv sync --locked --extra asr`. Both models load on demand.
Override the paths with `MOMOI_EMBEDDING_MODEL_PATH` and `MOMOI_ASR_MODEL_PATH`.
Windows uses its installation directory for the built-in ASR component.

In Settings, select Sherpa and leave its endpoint and model path empty to use
built-in speech recognition. Tencent Cloud and custom remote Sherpa services
remain selectable. The old default `http://asr:8003` migrates to in-process ASR;
after checking the upgrade, remove the old ASR container from custom deployments.
