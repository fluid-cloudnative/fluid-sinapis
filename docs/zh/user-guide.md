# 用户使用文档（下载 · 安装 · 配置 · 启动）

> English: [User Guide](../en/user-guide.md)
>
> 相关文档：[自定义策略与指标使用](custom-strategies-and-metrics.md) · [Example（用户系统模拟器）使用](example-usage.md)

Fluid-Sinapis-Agent 是一套基于大模型智能体构建的 Fluid 弹性缓存管理系统，通过
**多源指标定时感知 → LLM 诊断 → 选策略出方案 → LLM 复核 → 调 Fluid 执行 → 回检记录**
形成自治闭环。本文面向首次部署的用户，覆盖从下载代码到全链路跑通的完整流程。

---

## 目录

1. [系统组成](#1-系统组成)
2. [运行依赖](#2-运行依赖)
3. [下载代码](#3-下载代码)
4. [安装 Python 依赖](#4-安装-python-依赖)
5. [准备基础设施](#5-准备基础设施)
6. [配置详解](#6-配置详解)
7. [启动](#7-启动)
8. [验证运行状态](#8-验证运行状态)
9. [首次运行建议](#9-首次运行建议)
10. [常见问题](#10-常见问题)

---

## 1. 系统组成

系统由四个可独立启动的进程组成，共享同一份配置文件 `server/config.yml`（各读各的段）：

| 进程 | 入口 | 职责 |
|---|---|---|
| 采集器 | `server/collectors/run_collectors.py` | 周期拉取 Fluid 指标与 K8s 指标，统一封装后推送到 Pushgateway |
| HTTP 后端（Web 控制台） | `server/controller/web_controller.py` | 提供指标查询 API 与前端页面；内置用户系统外接推送接口 |
| 主 Agent | `server/agent/run_agents.py` | 周期读取时序指标 → LLM 推理出执行计划 → LLM 独立复核 → 调 Fluid 技能脚本执行 → 记录结构化结果 |
| 用户系统模拟器（可选） | `examples/user_system_simulator.py` | 模拟真实业务系统向 HTTP 后端推送访问事件/指标，用于本地联调 |

```
用户系统/模拟器 ──POST──▶ HTTP 后端 ──▶ Pushgateway ◀── 采集器（Fluid/K8s 指标）
                            │                 │
                            ▼                 ▼
                       Prometheus（读通道 :9090，抓取 Pushgateway :9091）
                            │
                            ▼
                     主 Agent（读指标 → 规划 → 复核 → 执行 Fluid 操作）
```

---

## 2. 运行依赖

| 依赖 | 版本要求 | 用途 |
|---|---|---|
| Python | **>= 3.12**（项目 `pyproject.toml` 声明 `requires-python >=3.12`） | 运行全部四个进程 |
| uv（推荐） | 最新版即可 | 创建虚拟环境、安装依赖；也可用原生 `python3.12 -m venv` 替代 |
| Kubernetes 集群 | >= 1.30 | 承载 Fluid 与缓存工作负载 |
| Fluid | >= 1.0.8 | 缓存引擎（Alluxio / JuiceFS 等） |
| Prometheus | 2.x | 指标读通道，Agent 与 Web 控制台通过 PromQL 查询 |
| Pushgateway | 最新版 | 指标写通道，采集器与外接推送都写到这里 |
| 大语言模型 | OpenAI 兼容接口 | 规划/复核两个角色各需一个模型（GLM、DeepSeek、SinapisAI 等均可） |
| Node.js | >= 20.19（可选） | 仅在需要从源码构建 Web 控制台前端（`web/dist`）时使用 |

职责边界：本系统**不替代** Fluid 缓存引擎、Prometheus 采集与 K8s 调度等基础设施，也不处理底层存储故障、网络分区等系统性故障。

---

## 3. 下载代码

```bash
git clone https://github.com/<org>/fluid-sinapis-agent.git
cd fluid-sinapis-agent
```

目录速览：

```
fluid-sinapis-agent/
├── server/
│   ├── config.yml            # 唯一配置文件（见第 6 节）
│   ├── collectors/           # 采集器（run_collectors.py）
│   ├── controller/           # Web 控制台 + 用户系统推送接口
│   ├── agent/                # Agent 循环、技能、执行器
│   │   └── skills/
│   │       ├── official/     # 内置策略技能（规划/复核用）
│   │       ├── review_skills/# 审核 Agent 专用技能
│   │       ├── fluid_skills/ # 操作型技能（含可执行脚本）
│   │       └── user_skills/  # 用户自定义策略（含 _template 模板）
│   └── requirements.txt      # 锁定版本的依赖清单
├── examples/                  # 用户系统模拟器
├── docs/                     # 文档（zh / en）
└── tests/
```

---

## 4. 安装 Python 依赖

推荐用 uv（安装 uv：`curl -LsSf https://astral.sh/uv/install.sh | sh`）：

```bash
uv venv --python 3.12
uv pip install -r server/requirements.txt -p .venv/bin/python
```

或用原生 venv：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
```

说明：

- `server/requirements.txt` 是由 `uv export` 从 `pyproject.toml` 生成的锁定版本清单，**优先用它**而不是 `uv sync`（工作区的 `uv.lock` 可能与导出状态不同步）；
- 之后所有进程统一用 `.venv/bin/python` 启动，避免误用系统 Python（通常是 3.10，装不了依赖）；
- 开发者另需 `pytest`、`ruff`：`uv pip install pytest ruff`，测试用 `.venv/bin/python -m pytest` 运行。

---

## 5. 准备基础设施

### 5.1 Kubernetes + Fluid

1. 本机 `kubeconfig` 中要存在配置指向的 context：

   ```bash
   kubectl config get-contexts        # 找到要用的 context 名
   kubectl get namespace              # 确认命名空间存在
   ```

2. 目标命名空间中已部署 Fluid 的 `Dataset` 与 `Runtime`（如 AlluxioRuntime），名字与
   `target.dataset` / `target.runtime_name` 一致：

   ```bash
   kubectl -n <namespace> get dataset
   kubectl -n <namespace> get alluxioruntime
   ```

### 5.2 Pushgateway 与 Prometheus

Pushgateway（写通道，默认 :9091）：

```bash
# 持久化可选，但建议开启，避免进程重启丢失最近样本
pushgateway --web.listen-address=:9091 --persistence.file=~/pushgateway.data
```

Prometheus（读通道，默认 :9090），在 `prometheus.yml` 里抓取 Pushgateway：

```yaml
scrape_configs:
  - job_name: pushgateway
    honor_labels: true          # 保留推送方携带的 source 等 label，必须开启
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:9091']
```

> `honor_labels: true` 很重要：不开启时 Prometheus 会把与抓取自身冲突的推送标签
> （如 Pushgateway 的 `job`）改名为 `exported_job` 前缀，按推送方分组的查询会失效；
> 开启后推送的 `source`、`job` 等标签原样保留。

### 5.3 模型 API 密钥

模型密钥**只放环境变量**，不进配置文件：

```bash
export LLM_API_KEY=<你的密钥>    # 与 config.yml 中 ${LLM_API_KEY} 占位符对应
```

建议写入 `~/.bashrc` 或启动脚本，Agent 进程启动时即可读到。

---

## 6. 配置详解

全部配置集中在 `server/config.yml`，由三个进程各读各的段，彼此不校验：

| 配置段 | 读取方 | 说明 |
|---|---|---|
| `target` | 采集器 / Web / Agent（共用） | 集群 context、命名空间、数据集、Runtime 的**唯一来源** |
| `k8s` | 采集器 / Web | 采集口径（节点角色、余量算法、超时） |
| `prometheus` | 采集器 / Web / Agent（共用） | `pushgateway_url` 写通道 + `query_url` 读通道 |
| `collectors` | 采集器 | 采集间隔 |
| `agent` | Agent | 循环间隔、是否启用二次复核 |
| `model` / `models` | Agent | 模型连接（共享默认值 + 按角色覆盖） |
| `skills` | Agent | 四类技能目录位置 |
| `execution` | Agent | 执行限制（路径白名单、超时、预演开关） |
| `policies` | Agent | 策略参数（扩容步长、预热的文件数/大小下限等） |
| `user_system` | Web 控制台（接收）/ Agent（观测） | 外接推送的指标/事件清单，**唯一支持热加载的段** |

### 6.1 关键配置项

```yaml
target:
  kube_context: kubernetes-admin@cluster   # kubectl config get-contexts 查看
  namespace: jgd-fluid
  dataset: nju-model-hdfs                  # Dataset 名
  runtime_name: nju-model-hdfs             # 留空 = 跟随 dataset
  runtime_plural: alluxioruntimes          # Runtime CRD 复数名

prometheus:
  pushgateway_url: http://localhost:9091   # 写
  query_url: http://localhost:9090         # 读

agent:
  interval_seconds: 60                     # 决策循环周期
  validation_enabled: true                 # false = 跳过第二次 LLM 复核

model:                                     # 共享默认值
  api_key: ${LLM_API_KEY}              # 只写环境变量名占位符
  base_url: https://open.bigmodel.cn/api/paas/v4
  name: glm-5.3-flash
  temperature: 0
  timeout_seconds: 300

models:                                    # 按角色覆盖（仅 planner/validator）
  planner:  { name: glm-5.3-flash }
  validator: { name: glm-5.3-flash }

execution:
  allowed_path_prefixes: [/models]         # preload/evict 允许的 UFS 路径前缀
  timeout_seconds: 660                     # 单脚本超时；超时=结果未知，不重试。必须大于 wait_timeout_seconds
  wait: true                               # 是否等异步操作真正完成（默认开启）
  wait_timeout_seconds: 600                # 单个脚本轮询完成的最长时间，由上方的超时兜住
  dry_run: false                           # 预演开关：true 只打印不落集群

policies:
  expand:  { replica_increment: 1 }
  preload: { max_files: 3, min_file_size_bytes: 1024 }
```

约定与规则：

- **优先级：环境变量 > `config.yml` > 代码默认值**；
- 同一语义的值只写一处（集群信息在 `target`、Prometheus 地址在 `prometheus`），避免多处漂移；
- 旧键（`k8s.namespace`、`k8s.kube_context`、`fluid.*`、`metrics.prometheus_url`、
  `prometheus.prometheus_query_url`）已合并到新段，文件里残留会**启动即报错**并提示新位置；
- 除 `user_system` 段外，改完配置需重启对应进程。

### 6.2 环境变量一览

| 环境变量 | 对应配置 | 默认 |
|---|---|---|
| `SINAPIS_CONFIG` | 配置文件路径（采集器/Web） | `server/config.yml` |
| `KUBE_CONTEXT` / `KUBE_NS` | `target.kube_context` / `target.namespace` | 见 config.yml |
| `FLUID_DATASET` / `FLUID_RUNTIME` / `FLUID_RUNTIME_PLURAL` | `target.*` | 见 config.yml |
| `K8S_NODE_ROLE` / `K8S_FREE_MODE` / `K8S_TIMEOUT_SECONDS` | `k8s.*` | worker / request / 10 |
| `PUSHGATEWAY_URL` / `PROMETHEUS_URL` / `PROMETHEUS_TIMEOUT_SECONDS` | `prometheus.*` | :9091 / :9090 / 10 |
| `COLLECTOR_INTERVAL_SECONDS` | `collectors.interval_seconds` | 10 |
| `LLM_API_KEY` 等 | `model.api_key` 的 `${...}` 占位符 | 无（未设置启动报错） |
| `WEB_HOST` / `WEB_PORT` | Web 监听地址 | 代码默认 127.0.0.1 / 8000；`bin/start.sh` 启动时默认改为 0.0.0.0（局域网可访问） |
| `WEB_CACHE_TTL_SECONDS` / `WEB_CORS_ORIGINS` | Web 缓存与跨域 | 5 / localhost |
| `WEB_CACHED_FILES_SCAN_PATH` / `WEB_CACHED_FILES_TTL_SECONDS` | 已缓存文件表的 Alluxio 扫描目录 / 扫描结果缓存时长 | /models / 30 秒 |
| `SINAPIS_USER_METRICS_CONFIG` | Web 控制台外接指标/事件定义的独立配置文件 | 默认同 `SINAPIS_CONFIG` 指向的文件 |
| `AGENT_LOG_FILE` | agent 阶段日志文件路径，置 `off` 关闭文件输出 | `<仓库>/logs/agent.log` |

---

## 7. 启动

### 方式一：一键脚本（推荐）

仓库提供 `bin/start.sh` / `bin/stop.sh` / `bin/restart.sh`，管理四个服务：
**collector**（采集器）、**backend**（HTTP 后端，自带 Web 页面与推送接口）、
**frontend**（前端 dev 服务，仅开发时需要）、**agent**（主服务）。

```bash
bin/start.sh                 # 启动全部（顺序：collector → backend → frontend → agent）
bin/start.sh backend agent   # 只启动指定服务
bin/stop.sh                  # 停止全部
bin/stop.sh backend          # 只停止指定服务
bin/restart.sh               # 重启全部
```

说明：

- PID 写入 `run/<服务名>.pid`，日志写入 `logs/<服务名>.log`，重复执行 start 会跳过已运行的服务；
- agent 启动前会检查 `LLM_API_KEY`，未设置会明确报错退出；
- backend 默认监听 `127.0.0.1:8000`，可用 `WEB_PORT=9000 bin/start.sh backend` 覆盖；
  frontend 默认 `127.0.0.1:5173`，可用 `FRONTEND_PORT` 覆盖；
- **接口数据始终来自真实集群采集**（无演示模式）；某区块采集失败时该区块返回 `error` 与原因，不影响其它区块；
- frontend：仅当 `web/node_modules` 存在时 `bin/start.sh frontend` 才会启动 vite dev server，否则跳过（退出码 0）——已构建 `web/dist` 时由 backend 直接托管页面；两者都没有时页面返回 503 及构建提示，直到执行 `cd web && npm install && npm run build`。

### agent 阶段日志（logs/agent.log）

agent 进程把一轮决策的关键阶段日志双写到 `logs/agent.log`（10MB×5 轮转，`AGENT_LOG_FILE`
可改路径或置 `off`）：每轮开始/结束（含 approved 与 execution 状态、耗时）、读取指标、
用户策略判定、LLM 推理与复核（含依据）、每个 Fluid 技能执行步骤、失败堆栈；
结构化报告 JSON（stdout）由 `bin/start.sh` 追加进同一文件，stderr 控制台副本在
`logs/agent-console.log`。跟踪方式：

```bash
tail -f logs/agent.log
```

### 方式二：手动命令

需要自定义参数或接 systemd 时，可在仓库根目录按顺序手动执行：

```bash
# ① 采集器：周期推送 Fluid/K8s 指标
.venv/bin/python server/collectors/run_collectors.py

# ② HTTP 后端（Web 控制台 + 外接推送接口），监听 127.0.0.1:8000
.venv/bin/python -m server.controller.web_controller

# ③ （可选，联调用）用户系统模拟器（--scenario 必填：preload 或 expand）
.venv/bin/python examples/user_system_simulator.py --scenario preload

# ④ 主 Agent：周期决策循环
.venv/bin/python -m server.agent.run_agents
```

其它要点：

- **Web 控制台必须用 `-m` 模块方式启动**（直接 `python server/controller/web_controller.py` 会因 import 路径失败）；采集器脚本自带路径处理，两种方式均可；
- Agent 可用 `--config <路径>` 指定其它配置文件，`--once` 只跑一轮决策（调试推荐）：
  ```bash
  .venv/bin/python -m server.agent.run_agents --once
  ```

---

## 8. 验证运行状态

```bash
# Web 后端存活
curl http://127.0.0.1:8000/api/healthz

# 指标总览（采集器推送后应有数据）
curl http://127.0.0.1:8000/api/overview

# 跟踪 agent 阶段日志（轮次 / 读指标 / 推理 / 复核 / 执行）
tail -f logs/agent.log

# 外接推送配置清单（含热加载状态）
curl http://127.0.0.1:8000/api/user-system/metrics/schema

# 分段查看 K8s / Fluid 指标
curl http://127.0.0.1:8000/api/k8s
curl http://127.0.0.1:8000/api/fluid
```

Prometheus 侧确认抓取正常：

```promql
up == 1                                       # pushgateway job 应为 1
{job="fluid_agent:fluid"}                     # 采集器推送的 Fluid 指标
```

Agent 侧看日志：正常一轮按顺序出现这些英文日志关键字
`Reading metrics from Prometheus → LLM planning → LLM review → Running Fluid skill → round N end`。
执行类操作每步会附 `exit_code` 与脚本 JSON 输出。

---

## 9. 首次运行建议

1. **先预演**：把 `execution.dry_run` 置为 `true` 跑一两轮，确认 Agent 规划的 manifest
   符合预期（只打印、不落集群），再改回 `false`；
2. **先单轮**：用 `run_agents --once` 验证「指标可读 → 模型可连 → 计划可执行」全链路，
   再放开周期循环；
3. **收敛权限**：`execution.allowed_path_prefixes` 覆盖 `target.dataset` 实际的 UFS
   路径即可，不要留空（留空=不限制）；
4. **复核别关**：`agent.validation_enabled: true` 让第二次 LLM 结合状态与操作规范独立
   复核计划，是压住模型误操作的关键开关；
5. **模型超时**：大上下文推理可能超过 2 分钟，`model.timeout_seconds` 建议 ≥300；
   想缩短单轮耗时可选非思考型模型档位。

---

## 10. 常见问题

**Q1：启动报 `k8s.namespace has moved to target.namespace`？**
旧配置键残留。按报错提示把值挪到新段并删除旧键，重启即可。

**Q2：启动报 `references the unset environment variable LLM_API_KEY`？**
config.yml 里的 `${...}` 占位符对应的环境变量没有 export。`export LLM_API_KEY=...`
后再启动 Agent。

**Q3：采集/推送时报 400（duplicate series）？**
改过指标 label/分组后，Pushgateway 里的旧分组会与新分组冲突（同 job 重复序列）。先
DELETE 旧分组再推，例如：
`curl -X DELETE http://localhost:9091/metrics/job/<job>/<grouping-key>`。

**Q4：连 K8s 报 SSL EOF / 连接失败，本机配了 `https_proxy`？**
kubernetes 34.x 客户端会把 `https_proxy` 读进连接配置并清掉 `no_proxy`，内部集群地址
（`*.local` 等）被代理劫持。取消代理环境变量，或确保代码里的直连旁路（bypass）生效后重试。

**Q5：Web 页面 503 `frontend build not found`？**
前端产物 `web/dist` 未构建：`cd web && npm install && npm run build`。仅用 API 可忽略。

**Q6：`uv sync` 报 lock 文件错误？**
工作区 `uv.lock` 若损坏（如条目缺 source 字段），先 `git checkout uv.lock` 恢复，或直接
改用第 4 节的 `requirements.txt` 安装方式。

**Q7：想换 Prometheus / Pushgateway 地址？**
改 `server/config.yml` 的 `prometheus` 段（或设 `PROMETHEUS_URL` / `PUSHGATEWAY_URL`
环境变量），三端共用同一处，改完重启采集器与 Agent。
