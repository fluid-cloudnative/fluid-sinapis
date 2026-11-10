# Example 使用文档（用户系统模拟器）

> English: [Example (User-System Simulator)](../en/example-usage.md)
>
> 相关文档：[用户使用文档](user-guide.md) · [自定义策略与指标使用](custom-strategies-and-metrics.md)

`examples/user_system_simulator.py` 是一个**只依赖 Python 标准库**的模拟器，用来扮演云上 MaaS
推理系统，向 Web 控制台推送模型文件访问事件与下载带宽指标。**只需两次推送**，就能让 Agent 走完
预加载 → 扩容 → 清理 → 缩容 的完整链路：扩容之后的一切都由缓存状态自己驱动，不需要再推数据。

---

## 1. 工作原理

模拟器**推送一次即退出**，不再循环。一次推送做两件事：

1. `POST /api/user-system/events`：为场景中的每个文件推送固定条数的 `model_file_access`
   访问事件（文件路径 + 访问时间 + 文件大小；条数由 `SCENARIO_ACCESSES` 决定：preload 为
   50，expand 为 60），文件大小是该文件在 HDFS 上的真实大小，因此 Agent 的容量计算是精确的；
2. `POST /api/user-system/metrics`：推送一条随机（20~120 MiB/s）的
   `model_download_bandwidth` 带宽指标。

推送哪些文件由 `--scenario` 决定，该参数**必填**：`preload` 或 `expand`。访问次数是每个场景
各自固定的突发值——它只是把文件标记为"热"，并不表达请求速率。`source` 标识默认为
`hdfs-simulator`——它会作为推送指标的标签，对事件则仅作记录（事件计数固定落在
`source=user_system` 分组下）。

## 2. 前置条件

- 按照[快速开始](quick-start.md)里说明启动各个服务；
- 准备数据：执行 `python examples/data_generate.py`，会在当前目录下生成六个数据文件，
  尺寸与模拟器上报的真实 HDFS 大小一致：

  ```
  big_model_104.zip  3152012255  (2.94GiB)
  big_model_105.zip  2643485774  (2.46GiB)
  big_model_106.zip  2830948396  (2.64GiB)
  big_model_107.zip  2841337723  (2.65GiB)
  big_model_108.zip  1985894795  (1.85GiB)
  big_model_109.zip  1671222590  (1.56GiB)
  ```

  将这六个文件上传到 HDFS，并创建挂载路径为 `/models/data` 的 Fluid Dataset，
  Dataset 挂载完成后再进行后续操作。开始前先确认环境：

  ```bash
  # Dataset 与 Runtime 已存在且绑定（-n 与名字按 target 配置段调整）
  kubectl -n <namespace> get dataset <dataset>
  kubectl -n <namespace> get alluxioruntime <runtime>
  ```

  后续步骤不符合预期时，最短的排查命令：

  ```bash
  kubectl -n <namespace> get dataload            # Agent 创建的 DataLoad（预加载）
  kubectl -n <namespace> get alluxioruntime <runtime> -o jsonpath='{.status.replicas}{"\n"}'
                                                 # 扩容/缩容后的副本数
  kubectl -n <namespace> get pods -l fluid.io/dataset=<namespace>-<dataset>
                                                 # worker Pod 是否 Ready
  ```
- 容量假设：**1 个 worker、5GiB 缓存**。108+109 合计 3.41GiB（占用 68%）装得下，
  再加上 104+105 就需要 8.80GiB，装不下。

## 3. 场景说明

| 场景 | 推送行为 | 期望的 Agent 决策 |
|---|---|---|
| `preload` | 一次推送：108、109 各 50 条访问 | 预加载 108+109（3.41GiB，占 5GiB 的 68%），不扩容，也没到 70% 的清理线 |
| `expand` | 在 108/109 已缓存的前提下，一次推送：104、105 各 60 条访问 | 8.80GiB 装不进 5GiB → 扩容到 `desired_replicas=2`；新副本 Ready 之后再预加载 104+105 |
| （无需第三次推送） | — | 10GiB 中已用 8.80GiB（88%），后续轮次越过 70% 清理线释放冷文件（约 2~3 个），剩下 1.56~2.94GiB（16%~29%）低于缩容线，于是缩掉多余的 worker |

所以完整演示就是：跑一次 `preload`，等 108/109 真正缓存完成，再跑一次 `expand`，然后让 Agent
一直跑着——清理与缩容会在接下来的轮次里自动出现。如果之前的实验把计数弄乱了，开始前跑一次
`python examples/clear_prometheus.py --yes` 即可。

## 4. 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--url` | `http://127.0.0.1:8000` | Web 控制台地址 |
| `--source` | `hdfs-simulator` | 推送方标识（作为推送指标标签；对事件仅作记录） |
| `--scenario` | 无（必填） | `preload` 或 `expand` |

访问次数是脚本里的模块常量 `SCENARIO_ACCESSES`（preload 50、expand 60），想换突发值直接改
这个字典；两个场景的次数相互独立，expand 的 104/105 会比 preload 的 108/109 更热。

## 5. 运行步骤

```bash
# 1. 启动 Web 控制台（如果还没起）
.venv/bin/python -m server.controller.web_controller

# 2. 加热 108/109，等 Agent 完成预加载
.venv/bin/python examples/user_system_simulator.py --scenario preload

# 3. 确认 108/109 已缓存后，再加热 104/105
.venv/bin/python examples/user_system_simulator.py --scenario expand
```

正常输出示例（程序输出为英文）：

```
config loaded: 1 metric, 1 event type (reload_error=None)
scenario=preload: 2 files x 50 accesses = 100 events, 3.41GiB (one push)
  pushed 50x big_model_108.zip (1.85GiB)
  pushed 50x big_model_109.zip (1.56GiB)
  expected: preload 108+109 = 3.41GiB of 5.00GiB per worker, no expansion
```

推送被拒绝或网络错误（含连接拒绝/超时）会打印失败原因并以非零码退出。

## 6. 观察自治闭环

两次推送完成后：

1. **Web 控制台**：打开 `http://127.0.0.1:8000` 查看 `/api/overview` 与
   `/api/fluid`、`/api/k8s` 的指标变化；
2. **Agent 日志**：启动主 Agent（`.venv/bin/python -m server.agent.run_agents`，或先
   `--once` 单轮）后，观察一轮内的完整流程：读取指标 → 用户策略判定（如有）→ LLM 推理
   → LLM 复核 → 执行技能脚本 → 报告；
3. **PromQL 验证数据落库**：

   ```promql
   rate(model_file_access_count[5m])          # 各文件访问频率
   model_file_last_access_time                # 各文件最近访问时间
   model_file_size_bytes                      # 文件大小
   model_download_bandwidth                   # 带宽
   ```

4. **执行动作**：若 `execution.dry_run: false`，可在集群里看到 DataLoad CRD（preload）、
   Runtime 副本变化（先 expand 后 shrink）以及缓存清理；`dry_run: true` 时只在 Agent
   日志里打印将提交的 manifest。

## 7. 常见问题

**Q1：模拟器启动就报 HTTPError 4xx？**
先查 `GET /api/user-system/metrics/schema`：事件/指标不在配置清单（`unknown_event` /
`unknown_metric`）或 `user_system` 段配置加载失败（503）。改好 config.yml 等 1 秒热加载后重试。

**Q2：推送成功但 Agent 没反应？**
Agent 决策有周期（默认 10 秒，`server/config.yml` 里的 `agent.interval_seconds`）且需要
观察窗口积累数据；确认 Agent 进程在跑、Prometheus 能查到
`rate(model_file_access_count[5m])`。用 `--once` 可立即触发一轮。

**Q3：`preload` 场景没有产生预加载？**
108+109 合计 3.41GiB，只有在缓存确实还有约 5GiB 空闲时才装得下——如果之前的运行已经把
104/105 或其他文件也缓存了，先清理 Fluid 缓存。另外确认 `execution.allowed_path_prefixes`
覆盖 `/models/data`（白名单之外的路径在执行前就会被拒绝），以及想要真实 DataLoad CRD 时
`execution.dry_run` 必须是 `false`。

**Q4：`expand` 场景没有触发扩容？**
两个前置条件：(1) 108/109 已经缓存——先跑 `preload` 并等它结束，只有一个 worker 用到 68%、
又来了 5.40GiB 的 104+105 时扩容才成为唯一选择；(2) 副本数还没到上限
（`policies.expand.max_workers`，默认 2）——到达上限后，缓存满的处置是 evict-cache 而不是扩容。
另外要多跑几轮：104/105 的预加载只在新 worker Ready 之后出现。

**Q5：模拟器和自己业务系统可以同时推吗？**
可以。推送指标按 source 分组、互不干扰；事件计数合并为全局单一计数，多个推送方同时推不会翻倍。同一 source 的旧分组改动
label 后要先 DELETE（见用户指南 FAQ Q3）。

**Q6：为什么没有 `evict` / `shrink` 场景？**
它们不需要额外推送。`expand` 之后两个 worker 的缓存是 8.80GiB / 10GiB（88%），已经在清理线
之上，接下来几轮 Agent 会自行释放冷文件；水位回落到缩容线以下后又会自动缩掉多余的 worker。
保持 Agent 运行、观察日志即可——这正是这套设计要演示的部分。
