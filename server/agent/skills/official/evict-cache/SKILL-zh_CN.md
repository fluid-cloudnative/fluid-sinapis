---
name: evict-cache
description: 副本已达上限且已使用缓存容量为70%左右、无法继续扩容时，清理冷数据缓存。
---

## 适用条件（两条都要满足）

- fluid_workers_desired 已达到 `context.policies.evict.max_workers`（默认 2）：副本数以
  fluid_workers_desired（spec.replicas）为基线，fluid_workers 只是 Ready 数，副本启动期间会
  滞后；
- usage_ratio = used_bytes / capacity_bytes 高于 `usage_ratio_above`（默认 0.70）。

未达上限时，缓存满的正确答案是 expand-cache，而不是清理；若热工作集仍在跨窗口扩张，应当
扩容而不是清理。

## 规则

- 每次清理3个文件，不到3个文件时全部清理
- 每轮至多一个 evict：{"action":"evict","parameters":{"path":"/models","target_bytes":N}}

## 与其他策略的关系
每轮至多一个 evict：若 shrink-cache 本轮已经规划了清理，不要再输出第二个。
