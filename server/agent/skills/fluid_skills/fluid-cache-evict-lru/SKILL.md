---
name: fluid-cache-evict-lru
description: Free files from the Alluxio cache in LRU order; clears cached copies only, never deletes UFS originals.
---

Exec `alluxio fs free` inside the Alluxio master Pod in LRU order: it clears the cache
only and never touches UFS; only files that are 100% cached are picked. free is
asynchronous and its exit code is unreliable, so success is decided by re-running ls after
the free and checking the measured cache ratio. `--force` force-clears unpersisted files
and risks data loss.
