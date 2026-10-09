# 测试

在仓库根目录运行 Python 测试。优先使用 Makefile 入口；它通过 uv 使用锁定的 `test` 依赖组（pytest、pytest-xdist）。

```bash
# 全量回归，默认 4 个独立进程并行
make test

# 指定文件或筛选测试
make test TEST_ARGS='tests/test_exec_tool.py'
make test TEST_ARGS='-k recall'

# 串行调试；单个测试或少量测试优先串行，避免进程启动开销
make test TEST_WORKERS=0 TEST_ARGS='tests/test_exec_tool.py -x'

# 查看最慢的测试
make test TEST_ARGS='--durations=20'
```

没有 make 时，等价命令为：

```bash
uv run --locked --group test pytest -q -n 4
```

不要再以 `uv run --with pytest pytest -q` 作为默认全量入口：它默认串行，也没有使用锁定的测试依赖组。需要串行运行时，显式设置 `TEST_WORKERS=0`（或 `-n 0`）。

测试必须支持进程隔离：文件和数据库使用各测试自己的临时目录，测试 HTTP 服务使用动态端口，避免共享固定路径或端口。不要为了加速跳过断言或缩减覆盖。

前端改动另运行 `npm run build` 验证构建；Python 测试不替代前端构建检查。

# 线上部署

Momoi 服务器通过 `ssh -p 2222 root@server` 访问。正式部署入口是服务器上的 `/root/docker-services/build/momoi/deploy.sh`（服务器本地文件，不在本仓库内）。

部署前完成必要测试并将待部署提交推送至 GitHub，然后通过 `ssh -p 2222 root@server 'bash -e /root/docker-services/build/momoi/deploy.sh'` 执行。脚本会在服务器仓库 `git pull`、使用 `Dockerfile.chrome` 构建 `momoi` 镜像，再到 `/root/docker-services` 执行 `docker compose up --build -d`。不要绕过此入口用容器内补丁、覆盖安装文件或叠加镜像代替正式部署。

部署后核对服务器 Git HEAD、容器使用的镜像、Dashboard `/api/health` 和频道连接日志。健康接口需要 Dashboard JWT，勿将令牌打印到日志。

# 架构文档

架构设计、解耦方案、阶段计划与设计审查文档仅在本地保留，不纳入 Git。统一放在已忽略的 `docs/architecture/` 下；现有 `docs/YYYY-MM-DD_*.md` 也保持忽略。已跟踪的此类文档使用 `git rm --cached` 取消跟踪，保留本地文件，不使用 `git add -f` 强制提交。面向用户的配置、使用和部署说明不属于这类内部设计文档，仍按原有方式维护。
