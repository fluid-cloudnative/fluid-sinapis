# 自定义策略与指标使用文档

> English: [Custom Strategies & Metrics](../en/custom-strategies-and-metrics.md)
>
> 相关文档：[用户使用文档](user-guide.md) · [Example（用户系统模拟器）使用](example-usage.md)

本文介绍系统的两个扩展点：

1. **自定义指标接入** —— 把你的业务系统数据接入指标管道（`config.yml` 的 `user_system` 段 + HTTP 推送接口）；
2. **自定义缓存策略** —— 注入你自己的缓存决策逻辑（`server/agent/skills/user_skills/` 目录，SKILL.md + 判定脚本）。

两者可以单独使用，也可以组合：业务指标推上来 → 自定义策略脚本读取这些指标做出判定 → Agent 规划与复核环节遵循该判定。

---

## 目录

- [第一部分：自定义指标接入（被动推送）](#第一部分自定义指标接入被动推送)
  - [两种接入方式](#两种接入方式)
  - [配置 user_system 段](#配置-user_system-段)
  - [推送接口](#推送接口)
  - [校验规则与错误码](#校验规则与错误码)
  - [事件派生指标与 PromQL 查询](#事件派生指标与-promql-查询)
  - [Agent 侧如何读取](#agent-侧如何读取)
- [第二部分：自定义缓存策略](#第二部分自定义缓存策略)
  - [目录结构与加载机制](#目录结构与加载机制)
  - [SKILL.md：策略语义](#skillmd策略语义)
  - [decide.py：判定脚本契约](#decidepy判定脚本契约)
  - [冲突规则](#冲突规则)
  - [完整示例](#完整示例)
  - [手工调试](#手工调试)

---

# 第一部分：自定义指标接入（被动推送）

无法被采集器主动拉取的业务数据（模型下载带宽、文件访问流水等）由用户系统**主动 POST**
到 Web 控制台，服务端校验后写入 Pushgateway，最终经 Prometheus 被 Agent 查询。

## 两种接入方式

| | 方式一：直接推送指标 | 方式二：推送事件 |
|---|---|---|
| 适用场景 | 用户系统自己算好了统计量 | 只能报"发生了一次访问"，频率等统计量交给服务端 |
| 接口 | `POST /api/user-system/metrics` | `POST /api/user-system/events` |
| 推送内容 | 指标名 + 数值（+ labels） | 事件名 + 维度 labels + 数值字段 fields |
| 落库结果 | 推什么存什么（gauge/counter） | counter 自动累计次数 + 数值字段落成 gauge（最近一次值） |
| 频率统计 | 自己算好再推 | 不用算，Prometheus `rate(counter[5m])` 直接查 |

## 配置 user_system 段

在 `server/config.yml` 的 `user_system` 段声明允许接入的指标与事件清单：

```yaml
user_system:
  # 方式一：直接推送的指标
  metrics:
    - name: model_download_bandwidth      # 须符合 Prometheus 命名规范
      help: 模型下载带宽（字节/秒）        # 必填，非空
      # type: gauge                       # gauge（默认）或 counter
      # labels: [source_id]               # 推送时必须携带的 label 键（可选）

  # 方式二：事件
  events:
    - name: model_file_access             # 事件名，推送方用它标识事件类型
      help: 模型文件访问事件
      labels: [file]                      # 事件必须携带的维度
      counter: model_file_access_count    # 服务端自动累计次数的 counter 指标名
      gauges:                             # 事件数值字段落库映射
        - field: access_time              # 事件里的字段名
          metric: model_file_last_access_time   # 落库的 gauge 指标名
          help: 模型文件最近访问时间（Unix 毫秒）
        - field: size_bytes
          metric: model_file_size_bytes
          help: 模型文件大小（字节）
```

**热加载**：修改本段后**无需重启** Web 控制台，服务端按文件修改时间检测，约 1 秒内生效。
配置解析失败时继续用旧配置服务（`reload_error` 会体现在 schema 接口里）。

命名规则：

- 指标名 `name` / `counter` / `metric`：`^[a-zA-Z_:][a-zA-Z0-9_:]*$`（Prometheus 规范）；
- label 键与事件字段名：`^[a-zA-Z_][a-zA-Z0-9_]*$`；
- 派生指标名重复（如两个事件派生出同名指标）会导致 Agent 启动即报错，注意起名唯一。

## 推送接口

### 方式一：`POST /api/user-system/metrics`

```bash
curl -X POST http://127.0.0.1:8000/api/user-system/metrics \
  -H "Content-Type: application/json" \
  -d '{
    "source": "model-service-01",
    "metrics": [
      {"name": "model_download_bandwidth", "value": 1048576.0}
    ]
  }'
```

- `source`：推送方标识，作为 Prometheus 分组键；
- `metrics[]`：`name` 必须在配置清单内；`value` 必须是有限数值；配置里声明了 `labels`
  的指标必须携带对应 label 键（未声明的额外键也会保留）。

### 方式二：`POST /api/user-system/events`

```bash
curl -X POST http://127.0.0.1:8000/api/user-system/events \
  -H "Content-Type: application/json" \
  -d '{
    "source": "model-service-01",
    "events": [
      {
        "name": "model_file_access",
        "labels": {"file": "/models/a.safetensors"},
        "fields": {"access_time": 1792982400000, "size_bytes": 1073741824}
      }
    ]
  }'
```

- `labels` 必须包含配置声明的全部维度；`fields` 必须包含全部 gauges 引用的字段且为有限数值；
- `counter` 指标名由服务端维护，**推送方不传**。

### 查询允许清单：`GET /api/user-system/metrics/schema`

返回当前生效的指标/事件配置（集成方可据此生成推送代码），以及热加载状态
（`config.metric_count` / `config.event_count` / `config.reload_error`）。

## 校验规则与错误码

两个接口均为**部分成功语义**：单条被拒绝不影响同批其它条目，响应里逐条给结果。

| status | reason_code | 含义 |
|---|---|---|
| accepted | — | 已接收并落库 |
| rejected | `unknown_metric` / `unknown_event` | 不在配置允许的清单里 |
| rejected | `missing_labels` | 缺少必需 label |
| rejected | `missing_fields` | 缺少 gauges 引用的字段 |
| rejected | `invalid_value` | 字段不是有限数值（NaN/Inf 等） |

整批写入 Pushgateway 失败时返回 **502**；`user_system` 段配置加载失败且无可用旧配置时返回 **503**。

## 事件派生指标与 PromQL 查询

每个事件由服务端自动维护两类指标：

- **counter**：按 (事件， labels) 维度累计发生次数。counter 随服务进程重启归零没有影响，
  `rate()` 会自动处理 reset；
- **gauges**：事件数值字段落成 gauge，保留最近一次的值。

派生指标写在独立的 pushgateway job（`fluid_agent:user_system_events`）里，与直接推送的指标（job `fluid_agent:user_system`）互不覆盖。

查询示例：

```promql
# 每个文件的访问频率（次/秒）
rate(model_file_access_count[5m])

# 最近 5 分钟访问最热的文件
topk(3, sum by (file) (rate(model_file_access_count[5m])))

# 文件大小与最近访问时间（最新值，落库值本身就是 Unix 毫秒）
model_file_size_bytes
model_file_last_access_time

# 直接推送的带宽指标
model_download_bandwidth
```

## Agent 侧如何读取

- Agent 从 Prometheus 读取最近 5 分钟内的最新样本，读取工具为 `read_user_system_metrics`；
- `config.yml` 的 `user_system` 段**非空时**该工具才会挂给 Agent，指标名清单在 Agent
  启动时校验（命名不合法/派生名冲突会启动报错）；
- 这些指标同时进入：planner 的观测上下文、自定义策略脚本的 `observations` 输入
  （见第二部分）。

---

# 第二部分：自定义缓存策略

内置策略（`expand-cache` / `preload-cache` / `shrink-cache`）用 Prompt 定义在
`server/agent/skills/official/`。你的自定义策略放在
`server/agent/skills/user_skills/` 下，由框架**每轮自动扫描加载、免重启**。

## 目录结构与加载机制

```
server/agent/skills/user_skills/
├── README.md                 # 目录约定说明
├── _template/                # 官方模板（下划线开头 → 不会被加载）
│   ├── SKILL.md              # 策略语义（给 planner / validator 看）
│   ├── README.md             # 人工参考手册（不进模型上下文）
│   └── scripts/
│       └── decide.py         # 确定性判定脚本
└── my-strategy/              # ← 你的策略：复制 _template 后改名
    ├── SKILL.md
    └── scripts/decide.py
```

加载规则：

- **目录名即策略名**；下划线开头的目录（如 `_template`）不会被加载；
- **循环模式下每轮重新扫描**：新增/修改策略下一轮自动生效，无需重启进程；`--once`
  模式重新执行即可；
- 目录不存在时按"当前没有自定义策略"处理，不报错；
- 每个策略的两部分各司其职：
  - `SKILL.md` 注入 **planner 与 validator** 的上下文（策略语义）；
  - `scripts/decide.py` 由框架每轮执行（确定性判定），判定结果作为最高优先级依据
    进入规划与复核。

## SKILL.md：策略语义

格式遵循 skill 规范：YAML frontmatter（`name` + `description`）+ Markdown 正文。
正文要写清楚（这是给模型看的，语义越明确，规划/复核越稳定）：

```markdown
---
name: my-strategy
description: 一句话说明策略做什么。
---

# <策略名>

## 策略语义（给 planner / validator 看）

- 触发条件：什么观测/指标满足什么条件时应该行动；
- 输出操作：`expand` / `preload` / `shrink` / `evict` 之一，参数遵循可执行操作目录的约定；
- 冲突规则：与内置策略对同一操作结论不同时，以本策略为准；
- 边界：什么情况下明确不动（action=null）。
```

## decide.py：判定脚本契约

框架每轮把**指标观测快照**从 stdin 喂给脚本，从 stdout 收一**行** JSON：

```
stdin:  {"observations": [{"tool": "...", "result": {...}}, ...],
         "context": {"target": {...}, "policies": {...}}}

stdout: {"action": "expand"|"preload"|"shrink"|"evict"|null,
         "parameters": {...}, "reason": "≤300字"}
```

约定细节：

| 项 | 说明 |
|---|---|
| `observations` | 与发给 planner 的观测同源：`read_fluid_metrics` / `read_k8s_metrics` / `read_user_system_metrics` 各一条，`result.metrics` 里是 `{"name", "labels", "value"}` 列表（含 `user_system` 自定义指标的最新值） |
| `context` | `target`（集群/数据集/Runtime）与 `policies`（config.yml 的 policies 段），与 planner 收到的相同 |
| `action` | 只能取可执行操作目录中的类型；`null` 表示本轮无表态 |
| `parameters` | `expand`/`shrink` → `{"desired_replicas": N}`（N 为目标总数）；`preload` → `{"paths": [...]}`（路径须来自真实观测且命中 `execution.allowed_path_prefixes` 白名单）；`evict` → `{"path": "/dir", "target_bytes": N}` 或 `{"count": N}`（`target_bytes`/`count` 二选一；`path` 是被扫描的目录，evict 为近似 LRU，无法指定单个文件） |
| `reason` | ≤300 字，会进入规划依据与日志 |
| 容错 | 非零退出 / 超时（60 秒）/ 坏 JSON 会被记录且**不阻塞流程** |
| 依赖 | 脚本与 agent 进程同权限运行；只使用标准库或 `.venv` 已有依赖 |

## 冲突规则

自定义策略与内置策略对**同一操作**（如 expand）给出不同结论时，**以自定义策略为准**。
该规则同时约束 planner（必须遵循）与 validator（必须核验）。目录中没有对应工具的自定义
action 不会报错，但也不是无代价：该步保持 unmapped，执行到此停下（后续步骤全部
skipped），整轮报告 `not_executed`。

策略参数（`context.policies`）在 `server/config.yml` 调整：

```yaml
policies:
  expand:  { replica_increment: 1 }              # 每次扩容默认步数
  preload: { max_files: 3, min_file_size_bytes: 1024 }   # 单轮预热的文件数上限/大小下限
```

## 完整示例

以下判定脚本在「下载带宽持续超过 100 MiB/s」时建议扩容（基于 `_template` 修改）：

```python
#!/usr/bin/env python3
import json, sys

BANDWIDTH_THRESHOLD_BYTES = 100 * 1024 * 1024

def flatten_metrics(observations):
    """把所有观测源的指标序列摊平成 {(指标名, labels): 值}。"""
    metrics = {}
    for observation in observations:
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        for metric in result.get("metrics", []):
            key = (str(metric.get("name")),
                   json.dumps(metric.get("labels", {}), sort_keys=True))
            if isinstance(metric.get("value"), (int, float)):
                metrics[key] = float(metric["value"])
    return metrics

def main() -> int:
    payload = json.load(sys.stdin)
    metrics = flatten_metrics(payload.get("observations", []))
    bandwidth = max((v for (name, _), v in metrics.items()
                     if name == "model_download_bandwidth"), default=0.0)
    if bandwidth > BANDWIDTH_THRESHOLD_BYTES:
        verdict = {"action": "expand",   # desired_replicas 交给 planner 结合 policies 计算
                   "parameters": {},
                   "reason": f"带宽 {bandwidth:.0f}B/s 超过阈值，建议扩容"}
    else:
        verdict = {"action": None, "reason": "带宽未达阈值"}
    print(json.dumps(verdict, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

配套的 SKILL.md 写清触发条件即可。使用步骤：

1. `cp -r server/agent/skills/user_skills/_template server/agent/skills/user_skills/bandwidth-expand`
2. 改 `SKILL.md` 的策略语义、`decide.py` 的判定规则；
3. 循环模式下一轮自动生效；`--once` 模式重新执行即可。

## 手工调试

```bash
# 空输入冒烟测试（期望输出一行 JSON，action=null）
echo '{"observations": [], "context": {}}' \
  | .venv/bin/python server/agent/skills/user_skills/bandwidth-expand/scripts/decide.py

# 带真实指标调试：把 agent 日志里的 observations 部分拷出来存成 obs.json
echo "{\"observations\": $(cat obs.json), \"context\": {}}" \
  | .venv/bin/python server/agent/skills/user_skills/bandwidth-expand/scripts/decide.py
```

判定结果会出现在 agent 日志的 `[user strategy] <策略名> verdict: action=<...>` 行，
以及 planner 输入的 `custom_strategy_verdicts` 字段里。
