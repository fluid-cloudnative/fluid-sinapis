"""Fluid CRD constants shared by the agent-side collectors.

DataLoad manifests are built by the preload skill script
(`fluid-cache-preload/scripts/preload.py`), which owns the naming rule; this module
only pins the API group/version the collectors read.
"""

GROUP, VERSION = "data.fluid.io", "v1alpha1"
