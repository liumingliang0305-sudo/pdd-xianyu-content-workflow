# 华为云与 Mac 部署约束

记录日期：2026-10-19。本文记录部署约束、实机检查及安装结果。用户确认后已通过国内源安装 Docker / Compose，并在 Mac 准备、上传和校验 n8n 离线镜像，云端基础服务已启动。它更新了原方案的同机部署假设，业务接口与生成工作流的相应调整仍需在实现前细化。可复用配置见 [deploy/cloud](../deploy/cloud/README.md)。

## 1. 已确认的服务器条件

通过已有 `huawei-cloud` SSH 配置检查；资源数字为安装前快照，软件版本为本次安装结果：

| 项目 | 结果 |
|---|---|
| 系统 | Ubuntu 24.04.4 LTS |
| CPU | 2 核，x86_64；云端容器目标平台为 linux/amd64 |
| 内存 | 系统可见 1775MiB；检查时 available 为 1414MiB |
| Swap | 未配置 |
| 根磁盘 | 40GB，约 35GB 可用 |
| Docker / Compose | 29.1.3 / 2.40.3，来自华为云 Ubuntu 源 |
| APT 源 | repo.huaweicloud.com/ubuntu |
| 已运行服务 | 系统服务及华为云代理；未发现正在运行的应用服务，未作完整应用盘点 |

已刷新 APT 索引并安装 docker.io、docker-compose-v2 及其依赖 containerd、runc；Docker 已设为随系统启动。没有执行全系统升级。

## 2. 服务放置

- 云端：单实例 n8n、它自身的 SQLite 持久化文件，以及轻量访问入口和必要连接组件。不启用 queue mode，不运行浏览器、业务 PostgreSQL 或 Redis。n8n 官方当前支持 SQLite，固定版本部署时再次验证。
- Mac：FastAPI、业务 PostgreSQL、Redis、Celery worker、Playwright、图片处理和导出；商品资料、草稿、图片和浏览器登录态主要留在本地。
- 云端只处理小型 JSON 和文件引用。控制执行历史保留量，避免把图片、完整网页或 Cookie 写入执行记录。
- 云端 2GB 是待验证的轻量调度预算，不是容量保证。记录运行峰值、剩余内存与 OOM；Mac 负责浏览器和业务计算。
- Mac 离线或休眠时暂停业务推进，恢复后从本地持久化状态继续。云端入口可访问不等于本地服务在线；未写入本地的导入请求不能返回“已接受”。

模型供应商尚未选定。原流程由 n8n 直接调用模型；如果云端无法访问所选模型，应把调用执行放到 Mac，并相应调整 WF-03、生成接口、预算和未知结果恢复规则。不得只把调用换到本地却绕过已有生成尝试记录。外部模型仍会收到必要内容，不能将“本地存储”描述为“内容完全不出本地”。

## 3. 网络检查结果

每个地址仅作一次短时、小响应探测，不下载镜像层或安装包；不证明长期稳定性。

| 地址 / 用途 | 本次结果 |
|---|---|
| 华为云 Ubuntu 源 noble/InRelease | HTTP 200 |
| registry-1.docker.io/v2/ | 连接超时 |
| auth.docker.io 公开镜像 token 接口 | 连接超时 |
| docker.n8n.io/v2/ | HTTP 401，说明入口可达，尚未验证认证及镜像层下载 |
| github.com 首页 | HTTP 200；不代表 release 附件可下载 |
| registry.npmjs.org/n8n/latest | HTTP 200；不代表所有包和安装脚本下载源可达 |
| download.docker.com 的 Ubuntu noble/InRelease | HTTP 200 |

结论：部分海外端点受限，不能笼统认为全部海外站点不可访问，也不能依赖 Docker Hub 直接拉取。

## 4. 下载与安装路径

1. **主机运行环境**：优先从服务器已配置的华为云 Ubuntu 源安装 Docker 和 Compose，先检查依赖、版本及磁盘空间。若安装依赖不可达，在匹配 Ubuntu 24.04 amd64 的环境准备完整 deb 依赖包，通过 SSH 上传。容器镜像包不包含宿主机 Docker 安装程序。
2. **应用镜像**：在能访问官方源的 Mac 或其他可信机器准备固定版本镜像。先明确目标平台 linux/amd64；Apple Silicon Mac 的本机 ARM 镜像不能直接当作该服务器的部署镜像。每次记录来源、版本、平台、源摘要及本地镜像 ID。
3. **离线归档**：用 `docker image save` 生成镜像归档，通过 SSH/SCP 上传，再用 `docker image load` 导入。导出前确认目标平台镜像已在本地；校验归档 SHA-256 与导入镜像 ID/平台。SHA-256 用于验证传输完整性，可信来源另行确认。
4. **启动不下载**：离线 Compose 使用明确的本地版本标签及 `pull_policy: never`，不带服务器端构建步骤，不用 latest。缺镜像时明确失败；启动脚本不执行 npm/pip 安装、远程脚本或浏览器下载。官方 save/load 支持保存镜像及标签，但不应假定归档恢复后所有 registry digest 元数据都可直接用于 Compose 引用。
5. **完整交付包**：应包含镜像归档、Compose 配置、非敏感环境模板、平台及版本清单、校验文件、安装和回退说明。密钥、真实环境文件、数据库及 Cookie 不进入交付包或 Git。
6. **后续升级**：继续使用联网环境准备、校验、上传、导入的流程，不让服务器自动追随最新版本。升级前备份 n8n 数据及凭据加密密钥；若升级发生数据库迁移，回退需恢复对应备份，不能仅换回旧镜像。

如果后续频繁更新，可再评估国内私有镜像仓库；首版不增加该依赖，也不依赖来源不明的公共镜像代理。

Mac 的 Python 依赖、Playwright 浏览器和系统依赖按 Mac 实际运行方式分别准备。云端 Linux 镜像与本地 macOS 安装包不可混用；浏览器安装包必须与锁定的 Playwright 版本匹配。

## 5. 运行网络与验收

- 镜像离线安装只解决安装阶段；Mac 与云端的业务连接、模型访问和证书续期都需要单独验证。
- 优先验证由 Mac 主动建立到云端的受控反向连接，避免要求家庭网络有固定公网 IP。云端转发入口仅供 n8n 所在网络访问，保留业务 API 鉴权；具体连接工具尚未选定，不依赖海外中继服务才能运行。
- n8n 管理入口通过受控访问方式提供，数据库、Redis、浏览器调试端口不对公网开放。页面静态资源不依赖海外 CDN。
- 安装后验证：无 registry 访问时重启成功；导入、轮询、审核可完成；Mac 断连时不重复调用模型或丢失任务；恢复后从原阶段继续；监测云端内存峰值与磁盘增长。
- SQLite 数据、n8n 加密密钥和本地业务数据分别备份并验证恢复；备份不能仅存放在原磁盘上。

## 6. 官方依据

- [n8n 数据库选择](https://docs.n8n.io/deploy/host-n8n/configure-n8n/choose-n8ns-database.md)：SQLite 与 PostgreSQL 支持。
- [Docker image save](https://docs.docker.com/reference/cli/docker/image/save/)与 [image load](https://docs.docker.com/reference/cli/docker/image/load/)：镜像归档及恢复；平台过滤参数有版本要求，实际命令按安装版本选择。
- [Compose 服务配置](https://docs.docker.com/reference/compose-file/services/)：platform 与 pull_policy。

## 7. 本次部署结果

- 云端目录 `/opt/pdd-xianyu/n8n`；n8n 2.43.8，平台 linux/amd64，镜像来源 docker.n8n.io/n8nio/n8n。版本来自准备时 npm 的 stable/latest 标签，随后固定版本拉取；清单见 [image-manifest.json](../deploy/cloud/image-manifest.json)。
- 镜像归档 271,861,002 字节，约 259MiB；服务器校验 SHA-256 通过，导入后平台和镜像 ID 与 Mac 记录一致。
- `pull_policy: never`，实际以 `--pull never --no-build` 启动及重建；未在云端拉取镜像。
- 仅监听云端 `127.0.0.1:5678`；Mac SSH 转发至 `127.0.0.1:15678`。Docker 随系统启动，容器采用 unless-stopped。SSH 隧道需在断开后重新建立。
- readiness 返回 HTTP 200；通过 SSH 的编辑器 HTTP 返回 200，设置接口表示尚需管理员首次初始化。浏览器打开被权限检查拒绝，未完成浏览器界面验证。
- 容器上限 1GiB、主 Node 堆上限 512MiB、内置 JS runner 堆上限 256MiB、生产执行和 runner 并发各 1。两次启动后快照约 624MiB 和 448MiB，后一次整机可用约 850MiB；不是压力测试或峰值保证。
- 已重建容器，并在备份时停机、重启，恢复为 healthy；SQLite 和凭据配置存放在绑定目录中。
- 已生成一致性备份 `n8n-20261019T094141Z-132205.tar.gz`，云端与 Mac 副本校验一致；Mac 受保护目录 `.local/cloud-backups/` 被 Git 忽略。解包后的 SQLite `integrity_check=ok`，包含 157 张表及加密配置。这是空实例备份完整性验证，不等于真实凭据和业务数据恢复演练。
- 尚未完成：用户自行设置管理员账号、Mac 业务服务、云端到 Mac 的业务连接、平台采集、模型接入及业务工作流。本次只部署云端 n8n 基础服务。
