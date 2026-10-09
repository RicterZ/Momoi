# Desktop Python host

`momoi_desktop/` owns desktop startup, workspace defaults, update snapshots,
Windows tool installation and the managed NapCat entry scripts. Its prompts,
emotions and redistribution licenses travel with this package.

The shared `src/momoi/` package does not import this package. Desktop startup
supplies the FFmpeg resolver through `momoi.integrations.media`; this preserves
on-demand Windows installation when the user switches speech providers.

From the repository root, run tests with `make test`. Pytest includes
`desktop/python` on its import path. A source backend can be started with
`uv run --locked python desktop/python/backend_entry.py` and the host's normal
workspace, installation directory and dashboard port arguments.

`packaging/windows/build_release.py` puts both Python packages and
`backend_entry.py` beside each other in the release's `app/` directory. Desktop
files are excluded from the shared Python wheel and Linux images. Changes to
paths used by the C# host require a matching shell and installer; the release
runtime identity includes the desktop package layout.
