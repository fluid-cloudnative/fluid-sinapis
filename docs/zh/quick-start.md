# 快速开始

> English: [Quick Start](../en/quick-start.md)
>
> 本文只保留最短路径。完整的下载/安装/配置说明与常见问题见 [用户使用文档](user-guide.md)；
> 相关文档：[自定义策略与指标](custom-strategies-and-metrics.md) · [Example（模拟器）使用](example-usage.md)

## 前置条件

开始前确认（细节见[用户使用文档](user-guide.md#5-准备基础设施)）：

- [ ] Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)（或原生 venv）
- [ ] Kubernetes 集群可用，kubeconfig 中有目标 context，且已装 Fluid（Dataset + Runtime 已建）
- [ ] Pushgateway（:9091）与 Prometheus（:9090，已配置抓取 Pushgateway、`honor_labels: true`）在运行
- [ ] 有一个 OpenAI 兼容的大模型 API 密钥（GLM / DeepSeek / SinapisAI 等）

## 1. 获取代码并安装依赖

```bash
git clone https://github.com/fluid-cloudnative/fluid-sinapis.git
cd fluid-sinapis
uv venv --python 3.12
uv pip install -r server/requirements.txt -p .venv/bin/python
```

可选（只有需要 Web 控制台页面时才要做）：构建一次前端产物。API 不依赖它；
没有 `web/dist` 时页面会返回 503：

```bash
cd web && npm ci && npm run build && cd ..
```

## 2. 最小配置

编辑 `server/config.yml` 的 `target` 段，改成你的集群信息：

```yaml
target:
  kube_context: kubernetes-admin@cluster   # kubectl config get-contexts 查看
  namespace: jgd-fluid                     # Dataset/Runtime 所在命名空间
  dataset: nju-model-hdfs                  # 你的 Dataset 名
  runtime_name: nju-model-hdfs             # 留空 = 跟随 dataset
```

导出模型密钥和 API Key：

```bash
export LLM_API_KEY=<你的密钥>
export SINAPIS_API_KEY=$(openssl rand -hex 32)   # 生成一次，然后持久化（见下）
```

> API Key 保护所有 HTTP 接口（请求头 `Authorization: Bearer`）。生成一次后写入部署环境
>（shell 配置、systemd `EnvironmentFile` 等）持久化——重启必须保持不变，且绝不能提交到
> git。推送方和 Web 控制台使用同一个 Key；详见[用户指南](user-guide.md#api-key-鉴权)。

## 3. 启动

推荐用仓库自带脚本（也可手动执行单个命令，见[用户使用文档](user-guide.md#7-启动)）：

```bash
bin/start.sh          # 启动全部：collector → backend → frontend → agent
```

- PID 写入 `run/`，日志写入 `logs/`；`bin/stop.sh` 停止、`bin/restart.sh` 重启；
- backend 未设置 `SINAPIS_API_KEY` 会拒绝启动，所有接口都要求
  `Authorization: Bearer <key>`（确需无 Key 的本机调试可用 `SINAPIS_AUTH_DISABLED=true`
  显式关闭）；
- backend 默认监听 `127.0.0.1:8000`，接口数据**始终来自真实集群采集**。保持默认监听
  地址，除非有意识暴露：Key 在纯 HTTP 下明文传输，暴露到局域网/公网需要 TLS 反向代理
  （`WEB_HOST=0.0.0.0` 会监听所有网卡）；
- agent 启动时若 `config.yml` 里的 `${...}` 占位符（如 `LLM_API_KEY`）对应的环境变量
  未设置，会启动即失败并明确报错；
- 首次建议先单轮验证再放开循环：
  ```bash
  .venv/bin/python -m server.agent.run_agents --once     # 手动单轮
  bin/restart.sh agent                                   # 或只重启 agent 服务
  ```

> 首次接入建议先把 `server/config.yml` 里 `execution.dry_run` 置为 `true` 预演一轮
> （只打印 manifest 不落集群），确认无误后再改回 `false`。

## 4. 验证

```bash
curl http://127.0.0.1:8000/api/healthz          # Web 后端存活
curl http://127.0.0.1:8000/api/overview         # 实时采集的指标总览
tail -f logs/agent.log                          # agent 阶段日志（轮次/读指标/推理/复核/执行）
```

Agent 日志应出现完整一轮（英文日志关键字）：`Reading metrics from Prometheus → LLM planning → LLM review → Running Fluid skill → round N end`。

## 5. 演示自治闭环（可选）

用模拟器扮演业务系统，依次触发预加载与扩容两种策略：

```bash
.venv/bin/python examples/user_system_simulator.py --scenario preload
```

详见 [Example 使用文档](example-usage.md)。

## 没有 K8s 集群？

系统没有演示模式：启动后接口的各区块会返回 `error` 与具体原因（连接失败等），
前端骨架仍可打开但无数据。想看有数据的页面需要自建集群。

## 下一步

- [用户使用文档](user-guide.md)：配置详解、环境变量、FAQ
- [自定义策略与指标](custom-strategies-and-metrics.md)：接入业务数据、编写自定义策略
- [Example 使用](example-usage.md)：模拟器场景与参数
