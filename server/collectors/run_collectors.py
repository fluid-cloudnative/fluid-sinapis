# server/collectors/run_collectors.py
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.agent.adapters.kubernetes import KubernetesAdapter
from server.collectors import FluidCollector, K8sCollector
from server.collectors.scheduler import CollectorScheduler
from server.config import AppConfig
from server.prometheus import configure

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

# -- 1. Connection settings all come from config.yml (env vars may override; see
#       server/config.py) --
cfg = AppConfig.load()
# Cluster / namespace / dataset are written only in the target section; an empty
# runtime_name follows dataset.
target = cfg.resolved_target()

# -- 2. One pipeline: writes go to the pushgateway, reads go through PromQL (the read
#       address shares the same key as the agent side) --
client, store = configure(
    pushgateway_url=cfg.prometheus.pushgateway_url,   # write: push collected results
    prometheus_query_url=cfg.prometheus.query_url,    # read: how strategies query metrics
)

adapter = KubernetesAdapter(target.kube_context,
                            request_timeout_seconds=cfg.k8s.request_timeout_seconds)

# Both collectors share the same complete target, so neither ends up with missing fields.
fluid = FluidCollector(adapter, target)
k8s = K8sCollector(adapter, target,
                   node_role=cfg.k8s.node_role, free_mode=cfg.k8s.free_mode)

sched = (CollectorScheduler(client, store, interval_seconds=cfg.collectors.interval_seconds)
         .register("fluid", fluid.collect)
         .register("k8s",   k8s.collect))

try:
    sched.start()
    print("Collectors started, press Ctrl-C to stop")
    import time as _t
    while True:
        _t.sleep(1)
except KeyboardInterrupt:
    sched.stop()
