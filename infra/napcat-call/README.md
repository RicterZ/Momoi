# NapCat 通话版镜像

基于固定版本的 NapCat 镜像，安装 Linux QQ AVSDK 桥、隔离 PulseAudio 与 Momoi 音频服务。
不包含 QQ 登录态、模型密钥或预设的 Bridge Token。首次启用时生成 Token，位于
`/app/qq-call/runtime/control.token`；通过受信任方式复制到 Momoi Dashboard。

```bash
docker compose -f infra/napcat-call/compose.yaml build
```

默认 `QQ_CALL_ENABLED=0`，仅验证原 NapCat 消息链路。完成 Momoi 端配置后设置为 `1`。
只有主人身份确认且 Momoi 保持就绪连接时才自动接听。Docker 内连接地址为
`http://napcat-call:6112`；宿主机测试用 `http://127.0.0.1:6112`。
原生 Bridge 的 6110/6111 端口不对外发布。

部署前对已有 QQ 数据及配置做快照；不要让两个实例同时使用同一账号/登录态。
测试账号验证来电、双向音频、挂断与容器重启后再替换生产容器。
原容器的 QQ 登录态、NapCat 配置卷可沿用，保留原镜像用于回滚。
QQ/AVSDK 升级必须重新验证，不能只更新到 latest。

上游：ClaudiaGardner/maibot-qq-voice-call，固定提交
`22f30c021cd3170f75af9dff66cc959a07ebda4b`，源码在镜像 `/opt/qq-call/upstream`，
许可证为 GPL-3.0-only。`prepare.py` 对该桥的修改同样按 GPL-3.0-only 提供；
没有将上游 MaiBot 业务插件并入 Momoi。
