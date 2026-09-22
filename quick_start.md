# 安装、启动与维护

适用于 Linux、macOS；Windows 请在 WSL 中执行。以下命令均在项目根目录运行。
当前工作台采用本机身份入口，适合受信用户通过本机或 SSH 隧道使用；不能直接开放公网。

## 当前 VPS 日常开发

通过 VS Code Remote SSH 或 SSH 进入现有工作目录，复用当前配置、虚拟环境和数据库：

```sh
cd /home/dev/projects/kg_abc2plus-remote
./quick_start.command
curl --fail http://127.0.0.1:8002/health/ready
```

`quick_start.command` 是 Linux、macOS 和 WSL 共用的启动入口，macOS 也可双击运行。
它调用 `scripts/restart_workbench.py`，读取 `.env.workbench.local`，启动或重启当前项目的
8002 服务，保持 `PLAYGROUND_PUMP_ONLY=1`，不执行初始化、资料导入或数据库重置。
关闭终端后服务继续运行；日志位于 `.local/workbench/8002.log`。

本机访问 <http://127.0.0.1:8002/industrial>；从自己电脑访问 VPS 时，按
[服务器访问说明](#6-部署到服务器) 建立 SSH 隧道。
若已采用下文的 systemd 服务，则使用 `sudo systemctl restart kg-workbench`，不要混用启动入口。

已有环境日常开发无需重复以下安装步骤。`.env.workbench.local`、`.local/` 和 `.venv/`
属于私密配置与运行环境，不提交到 Git，整理项目时保留。

## 1. 准备环境并克隆

需要 Git、Python 3.12+、Node.js 18+ 和 `lsof`；新建 Neo4j 时还需要可用的 Docker。
图谱前端依赖已包含在仓库中，无需执行 `npm install`。

```sh
git clone https://github.com/Jackson00Han/kg_abc2plus.git
cd kg_abc2plus
```

## 2. 安装依赖：uv 或 pip 二选一

**uv**：先安装 uv，再使用仓库锁定的依赖版本。

```sh
uv sync --locked
```

**pip**：使用 Python 3.12 或更新版本创建项目虚拟环境。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

pip 按 `pyproject.toml` 的版本范围安装，不读取 `uv.lock`。
两种方式都会得到 `.venv/`，后续命令相同。

## 3. 配置数据库与模型

首次创建配置；已有配置时保留并编辑原文件：

```sh
test -f .env.workbench.local || cp .env.example .env.workbench.local
chmod 600 .env.workbench.local
```

编辑 `.env.workbench.local`，填写数据库密码和有效模型配置：

| 配置 | 填写要求 |
| --- | --- |
| `PLAYGROUND_NEO4J_URI` | 同机数据库，例如 `bolt://127.0.0.1:7687`；使用显式回环 IP 和端口 |
| `PLAYGROUND_NEO4J_USER` / `PLAYGROUND_NEO4J_PASSWORD` | 新建容器使用 `neo4j` 和自设强密码；已有数据库使用其真实凭据 |
| `PLAYGROUND_NEO4J_DATABASE` | 当前入口使用 `neo4j` |
| `PLAYGROUND_PUMP_ONLY` | 保持 `1`；这是当前资料隔离开关，不会自动导入示例 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 同一地域、业务空间的有效 DashScope / MaaS Key 和官方 HTTPS 兼容接口 |
| `MODEL_NAME` | 该账号可用的聊天模型，用于本体约束抽取 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | 当前配置以 `text-embedding-v4`、`1024` 维为例，须与账号支持情况一致 |

抽取与向量化共用模型地址和 Key。当前入口只允许官方 DashScope / MaaS 地址，并非任意
OpenAI 兼容服务；可用维度为 64、128、256、512、768、1024、1536、2048。
环境文件已被 Git 忽略，勿提交凭据。

## 4. 准备 Neo4j

已有同机 Neo4j 5.26 数据库时跳过容器创建，并核对上一步连接配置。
全新安装可创建持久化容器。先从私密配置生成 Docker 环境文件，密码不会出现在命令参数中：

```sh
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from dotenv import dotenv_values

os.umask(0o077)
config = dotenv_values(".env.workbench.local")
password = config.get("PLAYGROUND_NEO4J_PASSWORD", "")
if config.get("PLAYGROUND_NEO4J_USER") != "neo4j":
    raise SystemExit("新建容器的数据库用户必须为 neo4j")
if len(password) < 12 or password.startswith("replace-") or any(c in password for c in "\r\n"):
    raise SystemExit("请先填写至少 12 位、不含换行的数据库密码")
Path(".local").mkdir(exist_ok=True)
with Path(".local/neo4j.env").open("x") as output:
    output.write("NEO4J_AUTH=neo4j/" + password + "\n")
    output.write("NEO4J_server_memory_heap_initial__size=512m\n")
    output.write("NEO4J_server_memory_heap_max__size=512m\n")
    output.write("NEO4J_server_memory_pagecache_size=512m\n")
PY
docker run -d --name kg-graphrag-neo4j --restart unless-stopped \
  --publish 127.0.0.1:7474:7474 --publish 127.0.0.1:7687:7687 \
  --env-file .local/neo4j.env \
  --volume kg-graphrag-neo4j-data:/data neo4j:5.26
```

环境文件生成命令遇到已有文件会停止，避免覆盖原密码。容器已创建时使用
`docker start kg-graphrag-neo4j`，不要重复创建或删除数据卷。
修改环境文件不会修改已有数据库的密码。

## 5. 初始化并启动

```sh
.venv/bin/python scripts/initialize_workbench.py --name "我的知识库"
./quick_start.command
curl --fail http://127.0.0.1:8002/health/ready
```

初始化会建立数据库结构、空知识库和向量索引；已有可见知识库会复用。
它不调用模型、不导入示例、不重置数据库。首次启动 Neo4j 可能需要等待一段时间。
健康检查返回 `status: ready` 后，打开 <http://127.0.0.1:8002/industrial>。
启动成功只代表服务与数据库就绪，模型调用还需用有效配置完成实际上传和检索验证。

在页面创建或选择知识库，导入本体、上传资料，再按构建、审核、发布流程使用。
可先使用 [循环水泵示例](src/graphrag_prod/playground/static/industrial-demo-v1/README.md)；
母线槽资料见 [输入说明](docs/busway-inputs.md)。

## 6. 部署到服务器

首次部署到新服务器时，完成上述安装、配置和初始化步骤。重启器启动的进程在后台运行，
关闭 SSH 不会停止；服务器重启后需要重新启动应用，或配置下方 systemd 服务。

在自己电脑的终端建立隧道，替换 SSH 用户和服务器地址：

```sh
ssh -N -L 127.0.0.1:18002:127.0.0.1:8002 your-user@your-server
```

保持隧道运行，访问 <http://127.0.0.1:18002/industrial>。
应用、模型调用和数据库均在服务器执行。当前身份入口可签发工作台角色会话，
不应通过端口开放或裸反向代理直接暴露；正式多用户部署需要另行接入身份认证。

### Linux 开机自启（可选）

使用普通用户运行应用。创建 `/etc/systemd/system/kg-workbench.service`，
将下面的用户与绝对路径替换为实际值：

```ini
[Unit]
Description=Knowledge workbench
After=network-online.target docker.service

[Service]
Type=simple
User=your-user
WorkingDirectory=/absolute/path/kg_abc2plus
EnvironmentFile=/absolute/path/kg_abc2plus/.env.workbench.local
ExecStart=/absolute/path/kg_abc2plus/.venv/bin/python /absolute/path/kg_abc2plus/scripts/run_playground.py --host 127.0.0.1 --port 8002 --no-open
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

`EnvironmentFile` 必须保留：直接运行前台入口不会自动读取 `.env.workbench.local`。
若已用重启器启动，先用 `lsof -nP -iTCP:8002 -sTCP:LISTEN` 核对进程，
正常停止本项目进程后再启用服务，避免两个入口争用端口。

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now kg-workbench
sudo systemctl status kg-workbench
```

此后通过 `sudo systemctl restart kg-workbench` 重启，
`sudo systemctl stop kg-workbench` 停止，`journalctl -u kg-workbench -n 60` 查看日志，
不再混用重启器。

## 维护与故障处理

- 启动与日志位置见 [当前 VPS 日常开发](#当前-vps-日常开发)；systemd 托管时按对应服务命令操作。
- 数据库不可达：检查 Docker、`docker logs kg-graphrag-neo4j` 和连接配置；已有数据库勿创建替代卷。
- 端口被占用：先确认进程归属；重启器不会关闭其他项目的服务。
- 模型请求失败：核对地域、Key、模型权限及余额；图谱布局失败则检查 `node --version`。
- 升级代码前备份 Neo4j 数据和私密配置；更新代码后，uv 执行 `uv sync --locked`，
  pip 执行 `.venv/bin/python -m pip install -e .`，按版本变更要求处理迁移，再重启检查。
- 不直接修改嵌入模型或维度来复用旧索引；已有知识需要对应的向量空间迁移与重建。
- 停止容器用 `docker stop kg-graphrag-neo4j`，恢复用 `docker start kg-graphrag-neo4j`；
  持久化数据在命名卷中。源码和输入文件不能替代数据库备份。
