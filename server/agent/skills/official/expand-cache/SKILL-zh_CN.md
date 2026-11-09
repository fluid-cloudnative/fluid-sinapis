---
name: expand-cache
description: 当已使用的缓存容量达到70%左右、或剩余容量已装不下当前热工作集时开始扩容，直至worker数量上限。
---

## 规则

- 满足任一条件即触发扩容：(a) 水位达到约 70%；(b) 剩余容量装不下当前热工作集——即活跃被访问文件的大小之和超过 `capacity_bytes - used_bytes`。判据要看工作集，不能只看比例：缓存完全可能处在 70% 以下却仍然放不进正在被请求的文件。而且水位只在本 Agent 动作之后才会变动，卡在阈值下方一点点的比例永远不会自己越线——那种状态下死等 70% 就永远不扩容；

- 收益很小时不扩容；空间充裕时，偶发的小文件优先走预热；
- 读取 `context.policies.expand`：replica_increment 默认 1；max_workers（默认 2）是副本数硬上限，与 `context.policies.evict.max_workers` 是同一个值，两者必须保持一致。
- 每轮至多一个 expand：{"action":"expand","parameters":{"desired_replicas":N}}，N 是目标总数（未知时返回 insufficient_data）；expand 排在 preload 之前；本轮不得规划依赖新增容量的预热。
