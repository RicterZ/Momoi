"""Version-checked additions shared by the NapCat and SnowLuma adapters."""


def patch_bridge(source):
    # Keep readiness gating identical regardless of the QQ host adapter.
    source = source.replace(
        'let acceptTimer = null;',
        'let acceptTimer = null;\nlet momoiReady = { ownerUin: "", until: 0 };',
        1,
    )
    needle = '  if (!Array.isArray(activeSDKInvite) || state.call.phase === "ended") return;'
    assert needle in source
    source = source.replace(
        needle,
        needle + '\n  if (Date.now() >= momoiReady.until || !state.call.callerUin || state.call.callerUin !== momoiReady.ownerUin) return;',
        1,
    )
    needle = '    if (req.method === "GET" && url.pathname === "/v1/status") {'
    assert needle in source
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
    source = source.replace(needle, addition + needle, 1)

    # Native command 20050 carries diagnostic text, not an AVSDK expiry event.
    needle = '(command === 20050 || command === 120043)'
    assert source.count(needle) == 1
    source = source.replace(needle, '(command === 120043)', 1)
    source = source.replace(
        '    loginPosted: false,',
        '    loginPosted: false,\n    loginSucceeded: false,',
        1,
    )
    source = source.replace(
        '    state.avHost.loginPosted = false;',
        '    state.avHost.loginPosted = false;\n    state.avHost.loginSucceeded = false;',
        1,
    )
    needle = '  state.avHost.lastOutputCommand = command;'
    assert source.count(needle) == 1
    source = source.replace(
        needle,
        needle + "\n  if (command === 1) state.avHost.loginSucceeded = Array.isArray(value) && value[0] === 0;",
        1,
    )
    source = source.replace(
        needle,
        needle + "\n  state.avHost.outputCommands ??= {};\n  state.avHost.outputCommands[command] = (state.avHost.outputCommands[command] || 0) + 1;\n  if ([1, 55, 20050, 120043, 20006].includes(command)) {\n    state.avHost.outputResults ??= {};\n    state.avHost.outputResults[command] = summarizeValue(value);\n  }",
        1,
    )
    return source
