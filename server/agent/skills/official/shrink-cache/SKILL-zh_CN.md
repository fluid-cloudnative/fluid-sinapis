---
name: shrink-cache
description: 当期望副本数（fluid_workers_desired）不为1，且已使用缓存容量低于35%时，执行缓存容量缩容策略。
---


## 规则

- 只根据Fluid系统指标和K8s集群指标来做判断
- 每轮至多减少1个worker，绝不低于 min_workers；{"action":"shrink","parameters":{"desired_replicas":N}}，N 必须低于 fluid_workers_desired（已知的期望副本数）；fluid_workers 只是 Ready 数，副本基线一律读 fluid_workers_desired；
- 本轮已规划 expand 时绝不缩容：若 expand-cache 本轮已规划扩容，这里什么都不做。水位以当前总容量为分母，扩容后的那一轮，光是新增副本就会让比例近乎腰斩，据此缩容等于把刚做的扩容推翻并再次触发它。缩容线与"扩容后的水位"之间要留有几个百分点的余量；
- 副本调整进行中不缩容（fluid_workers 低于 fluid_workers_desired，即新副本尚未 Ready）；
- 低水位必须是持续的，而不是刚被造出来的：只有副本数已连续若干轮未变化时才缩容。若观测里没有这段历史，返回 noop 而不是猜测——确定性的冷却应放在自定义 decide.py 里；
- shrink 排在 evict 之后；若缩容依赖清理完成，本轮只规划 evict；热工作集仍在跨窗口扩张时不要使用本策略。

## 与其他策略的关系

每轮至多一个 evict：若 evict-cache 本轮已经规划了清理，不要再输出第二个——若本策略自身
条件成立就只缩容，否则本轮什么都不做。水位高且已达副本上限属于 evict-cache 的处置范围；
本策略只在水位确实很低时才缩容。
