# 云端 n8n

目标：华为云 Ubuntu 24.04 amd64，2 核 2GB。单实例 n8n 2.43.8，SQLite；容器上限 1GB、Node 堆上限 512MB。没有部署业务采集服务或业务工作流。

## 访问

在配置了 `huawei-cloud` SSH 别名的 Mac 上执行：

```bash
bash deploy/cloud/tunnel.sh
```

保持终端运行，然后访问 <http://localhost:15678/>，首次打开创建自己的管理员账号。使用支持 localhost 安全 Cookie 的浏览器，例如 Chrome；保留 n8n 默认安全 Cookie 设置。隧道仅绑定本机回环，云端端口也仅绑定回环。当前不提供公网 webhook，外部 OAuth 回调须另行设计访问地址。

云端部署目录为 `/opt/pdd-xianyu/n8n`。主要文件：

- `compose.yaml`：固定镜像版本，禁止启动时自动拉取。
- `.env`：服务器生成的凭据加密密钥，权限 600，不输出到日志或 Git。
- `data/`：SQLite、配置及二进制文件，容器 UID 1000 所有。
- `image-manifest.json`：镜像来源、平台、版本及摘要。
- `backup.sh`：停服务后生成一致性备份，再启动服务。

## 镜像准备与安装

Mac 的 Docker 必须可用。下面命令用于联网准备机，服务器不需要访问 Docker Hub：

```bash
mkdir -p .local/cloud-bundle
docker pull --platform linux/amd64 docker.n8n.io/n8nio/n8n:2.43.8
set -o pipefail
docker image save --platform linux/amd64 docker.n8n.io/n8nio/n8n:2.43.8 | gzip > .local/cloud-bundle/n8n-2.43.8-amd64.tar.gz
cd .local/cloud-bundle
shasum -a 256 n8n-2.43.8-amd64.tar.gz > SHA256SUMS
```

使用支持平台参数的 Docker；本次 Mac 29.4.0、服务器 29.1.3。上传归档、校验文件和部署配置后，在服务器部署目录执行：

```bash
sha256sum -c SHA256SUMS
docker image load -i n8n-2.43.8-amd64.tar.gz
docker image inspect --platform linux/amd64 docker.n8n.io/n8nio/n8n:2.43.8
docker compose config --quiet
docker compose up -d --pull never --no-build
docker compose ps
curl -fsS http://127.0.0.1:5678/healthz/readiness
```

首次部署需预先创建 data 目录（UID/GID 1000）和权限 600 的 `.env`，用 `secrets.token_hex(32)` 生成一次密钥。已有 `.env` 绝不能重新生成，否则原凭据可能无法解密。不要运行会打印完整环境值的 `docker compose config` 或容器完整 inspect。

## 维护与备份

在云端部署目录执行 `bash backup.sh`，备份包含数据库、加密密钥、配置和镜像清单。文件敏感，只通过 SSH 传输到 Mac 的受保护目录，不上传 Git。脚本会短暂停止服务，适合没有任务运行时使用。

升级前备份，把固定版本的新镜像提前导入，更新 Compose 后再启动；不使用自动更新器。回退涉及数据库迁移时，停服务后恢复相应备份并使用原镜像，而非只修改版本号。覆盖恢复之前先保留当前 data 和 .env。恢复后核对目录 UID/GID、权限及 readiness。

执行成功数据不保存，失败数据按 7 天、1000 条的阈值定期清理（不是即时硬上限）；不要把 Cookie 或完整网页传入节点。日志轮转为 3 × 10MB。版本通知关闭，需要维护时主动检查官方安全更新。

未部署 n8n Assistant 沙箱、浏览器或额外 task runner 服务。当前镜像内置 JS runner，Python runner 因未安装 Python 而不可用；这不影响编辑器及 HTTP 编排，业务 Python 任务仍计划在 Mac 执行。内置 runner 模式在此版本已标记弃用，后续升级或开放给其他用户运行 Code 节点前需重新评估外部 runner 部署及资源。当前安装验证不代表完整业务流程已可用。

首次空实例已做停机备份及 Mac 副本校验，SQLite 完整性检查通过。创建账号、录入凭据或修改工作流后需重新备份；目前没有设置自动备份任务。
