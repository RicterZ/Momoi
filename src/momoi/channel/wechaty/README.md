# Wechaty channel worker

This worker is an opt-in experimental personal-WeChat bridge. It is not an
official WeChat integration. WeChat may warn, restrict, or ban accounts using
unofficial automation protocols. Use a test account only.

The worker is managed by Momoi and communicates over JSON lines on stdin/stdout.
It uses Wechaty with `wechaty-puppet-wechat4u`; Node.js and its dependencies must
be installed in the runtime image. Login state is kept in the channel data dir.
