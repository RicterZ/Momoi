# AudioRoute PoC

独立于 Momoi 的 Windows .NET 音频路由实验。没有 Python、Momoi 项目或管理员权限依赖。

构建：

```sh
dotnet publish tools/AudioRoutePoc/AudioRoutePoc.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o build/audio-route-poc
```

运行 AudioRoutePoc.exe，选择私有 QQ 进程（路径包含 Momoi/runtime/qq-call）及虚拟输入输出。
先点击“读取应用路由”，成功后点击“应用并验证”。重新拨打电话验证双向通话，同时系统默认设备保持真实音响和麦克风。
QQ 宿主与 Pepper Plugin 可能是不同 PID，可逐个测试。应用路由可能按可执行文件身份共享，因此不要选普通 Tencent QQ。

设置前保存 routing-backup.json；恢复、正常关闭时仅恢复仍等于 PoC 设置的项，不覆盖用户后来做的修改。
异常退出保留备份。若目标进程退出，先重新启动同路径 QQ 并刷新，再点击恢复。
日志为同目录 poc.log。回读成功仅代表 Windows 保存了偏好，不代表 QQ 音频引擎实际遵循。
读取失败会停止应用，不把 E_INVALIDARG 猜成“未设置”，也不会调用系统默认设备切换或全局重置 API。

使用未公开的 Windows.Media.Internal.AudioPolicyConfig 接口。ABI 参考 EarTrumpet，许可证见 EarTrumpet-LICENSE.txt。
