"""Apply version-checked upstream additions; preserve its original entrypoint."""
from pathlib import Path

root = Path('/opt/qq-call')
plugin = root / 'upstream/bridge/napcat-plugin/index.mjs'
s = plugin.read_text()
# This addition to the GPL Bridge remains an independently distributed patch.
s = s.replace('let acceptTimer = null;', 'let acceptTimer = null;\nlet momoiReady = { ownerUin: "", until: 0 };', 1)
needle = '  if (!Array.isArray(activeSDKInvite) || state.call.phase === "ended") return;'
assert needle in s
s = s.replace(needle, needle + '\n  if (Date.now() >= momoiReady.until || !state.call.callerUin || state.call.callerUin !== momoiReady.ownerUin) return;', 1)
needle = '    if (req.method === "GET" && url.pathname === "/v1/status") {'
assert needle in s
addition = '''    if (req.method === "POST" && url.pathname === "/v1/momoi/ready") {
      try {
        const body = await readJsonBody(req);
        if (typeof body.ownerUin !== "string" || !/^[0-9]+$/.test(body.ownerUin) || typeof body.ready !== "boolean") {
          return sendJson(res, 400, { code: -1, message: "invalid readiness" });
        }
        momoiReady = { ownerUin: body.ownerUin, until: body.ready ? Date.now() + 4000 : 0 };
        if (body.ready && activeSDKInvite && state.call.phase === "ringing") scheduleAccept(0);
        return sendJson(res, 200, { code: 0 });
      } catch { return sendJson(res, 400, { code: -1, message: "invalid readiness" }); }
    }
'''
s = s.replace(needle, addition + needle, 1)
plugin.write_text(s)
entry = Path('/app/entrypoint.sh')
s = entry.read_text()
needle = 'cd /app/napcat'
assert needle in s
s = s.replace(needle, 'bash /opt/qq-call/infra/bootstrap.sh\n' + needle, 1)
(root / 'entrypoint-upstream.sh').write_text(s)

# This pinned NapCat build only loads its plugin allowlist. Add our pinned bridge.
napcat = Path('/app/napcat/napcat.mjs')
s = napcat.read_text()
needle = 'new Set([\n  "napcat-plugin-builtin",'
assert s.count(needle) == 1, 'NapCat plugin allowlist changed; review required'
s = s.replace(needle, 'new Set([\n  "napcat-plugin-maibot-qq-voice-call",\n  "napcat-plugin-builtin",', 1)
napcat.write_text(s)
