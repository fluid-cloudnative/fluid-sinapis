"""Workflow-specific decision fixtures."""

import pytest


@pytest.fixture
def decision():
    return {
        "status": "action",
        "reason": "Add capacity and preload files that fit the currently available space",
        "steps": [
            {"action": "expand", "parameters": {"desired_replicas": 2}},
            {"action": "preload", "parameters": {"paths": ["/models/a.bin", "/models/b.bin"]}},
        ],
    }
