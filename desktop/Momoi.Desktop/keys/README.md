`update-public.bin` 是编入外壳的 Ed25519 原始公钥（32 字节）。
`update-private.pem` 是本地 PKCS#8 签名私钥，已加入 `.gitignore`，不随安装包发布。

私钥必须另外备份。丢失后无法为已安装外壳签发更新；轮换公钥需要发行新版外壳。
首次生成使用 `uv run --locked python packaging/windows/generate_update_key.py`。
脚本拒绝覆盖已有密钥。
