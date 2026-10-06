from pathlib import Path

# This pinned NapCat build only loads its plugin allowlist. Add our pinned bridge.
napcat = Path('/app/napcat/napcat.mjs')
s = napcat.read_text()
needle = 'new Set([\n  "napcat-plugin-builtin",'
if '"napcat-plugin-maibot-qq-voice-call",\n  "napcat-plugin-builtin"' in s:
    raise SystemExit(0)
assert s.count(needle) == 1, 'NapCat plugin allowlist changed; review required'
s = s.replace(needle, 'new Set([\n  "napcat-plugin-maibot-qq-voice-call",\n  "napcat-plugin-builtin",', 1)
napcat.write_text(s)
