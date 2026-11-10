# Fluid-Sinapis Web 控制台

Vue 3 + Vite 开发的仪表盘，展示 `server/collectors` 采集的两类指标：

- **K8s 集群信息**：节点数、可分配内存总量、空闲内存、Ready Pod 数、内存分配率（推导）
- **Fluid 系统信息**：缓存总容量、已用缓存、缓存使用率、单 Worker 平均容量、Ready Worker 数

数据接口由 `server/controller/web_controller.py` 提供（FastAPI），生产模式下由后端直接托管本目录构建产物。

控制台支持中英双语，默认英文，可在页面顶部 EN / 中文 切换，选择保存在 localStorage（key `sinapis.lang`）。文案集中在 `src/i18n.js`；指标名 / 区块 key 在这里映射双语文案，未知指标回退到后端下发的 `label` / `help` 字段。新增 collector 指标时请同步在 `i18n.js` 里补名字。

## 目录结构

```
web/
├── index.html            # 入口 HTML（默认 lang="en"，运行时随语言切换更新）
├── vite.config.js        # Vite 配置（dev 代理 /api → 8000）
├── public/favicon.svg
└── src/
    ├── main.js           # 应用入口
    ├── App.vue           # 布局、轮询、横幅、骨架屏、语言切换
    ├── api.js            # /api 请求封装
    ├── i18n.js           # 中英文案、指标名映射、语言状态（localStorage 持久化）
    ├── style.css         # 全局样式
    ├── utils/format.js   # 字节 / 百分比 / 时间格式化（locale 跟随界面语言）
    └── components/
        ├── SectionPanel.vue   # 单个数据源面板（含错误态、原始指标表）
        ├── CachedFilesPanel.vue # 已缓存文件列表
        ├── MetricCard.vue     # 指标卡片
        ├── UsageRing.vue      # 使用率环形图
        └── MetricIcon.vue     # 指标图标
```

## 开发

```bash
# 1. 启动后端（终端 1；依赖按 user-guide 第 4 节装进 .venv）
.venv/bin/python -m server.controller.web_controller

# 2. 启动前端 dev server（终端 2，/api 自动代理到 8000）
cd web
npm ci          # 按已提交的 package-lock.json 可复现安装；仅增删依赖时才用 npm install 并提交新的锁文件
npm run dev     # http://localhost:5173
```

## 生产构建

```bash
cd web && npm ci && npm run build
```

构建产物输出到 `web/dist`，后端启动后直接访问 `http://127.0.0.1:8000/` 即可，无需单独部署前端。

## 后端配置

与 `run_collectors.py` 共用的连接配置（集群 / 采集语义 / Prometheus）统一在 `server/config.yml` 里改（`target` / `k8s` / `prometheus` / `collectors` 四段，启动时读取，改完需重启）；下表的环境变量仍可用且优先级更高：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8000` | 服务监听地址（仅环境变量） |
| `SINAPIS_API_KEY` | 无（未设置拒绝启动） | 所有接口要求的 API Key（`Authorization: Bearer`），生成一次长期使用，部署环境中持久化、不进 git |
| `SINAPIS_AUTH_DISABLED` | 无 | 显式无 Key 逃生开关（仅本机调试），与 `SINAPIS_API_KEY` 互斥 |
| `KUBE_CONTEXT` | `kubernetes-admin@cluster` | kubeconfig context |
| `KUBE_NS` | `jgd-fluid` | 目标 namespace |
| `FLUID_DATASET` / `FLUID_RUNTIME` | `nju-model-hdfs` | 数据集与 Runtime 名 |
| `FLUID_RUNTIME_PLURAL` | `alluxioruntimes` | Runtime CRD 复数名 |
| `K8S_FREE_MODE` | `request` | `usage` 需 metrics-server |
| `WEB_CACHE_TTL_SECONDS` | `5` | 采集结果缓存秒数 |

## 接口

> **⚠ 安全提示**：以下接口均要求 `Authorization: Bearer $SINAPIS_API_KEY`（未带或错误
> 返回 401）。Key 在纯 HTTP 下明文传输，请保持 `WEB_HOST=127.0.0.1`（代码与
> `bin/start.sh` 的默认值），不要把端口直接暴露到公网；确需局域网访问时，先在前面加
> 一层 TLS 反向代理（详见 `docs/*/user-guide.md` 第 7 节「API Key 鉴权」）。

- `GET /api/overview` — K8s + Fluid 两个区块一次返回
- `GET /api/k8s`、`GET /api/fluid` — 单个数据源
- `POST /api/user-system/metrics` — 用户系统直接推送指标（被动接入，配置热加载，见根 README）
- `POST /api/user-system/events` — 用户系统推送事件流，服务端自动派生 counter/gauge（如文件访问事件 → 频率用 `rate()` 算）
- `GET /api/user-system/metrics/schema` — 当前允许的指标与事件清单
- `GET /api/healthz` — 健康检查
- `GET /api/docs` — OpenAPI 文档

单个数据源采集失败只影响自己的区块（`status: "error"` + 可读原因），另一个区块照常返回；采集结果按 TTL 缓存，一个刷新周期最多打一次 K8s API。
