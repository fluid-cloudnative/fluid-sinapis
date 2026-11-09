---
name: preload-cache
description: 按访问热度与剩余容量挑出收益最高的一组文件，输出预热方案。
---

## 规则

- 读取 `context.policies.preload`：max_files 默认 3（硬上限），min_file_size_bytes 默认 1024；
- 按访问频率与文件大小挑出收益最高的组合；不必凑满名额；选中项总和必须落在预算内；
- 仅当持续需求超过容量时才考虑 expand-cache；不得提出依赖"尚未扩容出来"的容量的预热；
- 每轮至多一个 preload：{"action":"preload","parameters":{"paths":["/dataset/path"]}}；
- 路径必须来自真实观测。
