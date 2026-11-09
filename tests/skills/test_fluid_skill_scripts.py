"""Fluid skill script contracts: pure-function behaviour plus the SKILL.md frontmatter rules.

skill directory names contain hyphens and cannot be imported as packages, so importlib
loads them by file path.
"""

import importlib.util
from pathlib import Path

import pytest

SKILLS_ROOT = (
    Path(__file__).resolve().parents[2] / "server" / "agent" / "skills" / "fluid_skills"
)
SKILL_NAMES = ("fluid-cache-preload", "fluid-cache-scale-up", "fluid-cache-evict-lru")


def _load(skill: str, script: str):
    """Load a skill script module by file path."""
    path = SKILLS_ROOT / skill / "scripts" / script
    spec = importlib.util.spec_from_file_location(
        f"skill_{skill.replace('-', '_')}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ══════════════════════════════════════════════════════════════
# SKILL.md contract: enforce the frontmatter requirements before handing it to LangChain
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_skill_md_frontmatter_is_well_formed(skill):
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill}: SKILL.md is missing its frontmatter"
    frontmatter = text.split("---", 2)[1]
    name = next(
        line.split(":", 1)[1].strip()
        for line in frontmatter.splitlines()
        if line.startswith("name:")
    )
    assert name == skill, f"{skill}: frontmatter name does not match the directory name"
    description = next(
        line.split(":", 1)[1].strip()
        for line in frontmatter.splitlines()
        if line.startswith("description:")
    )
    assert len(description) >= 10, (
        f"{skill}: description is too short for the LLM to tell when to use it"
    )


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_skill_script_is_compilable_with_a_shebang(skill):
    """The executor always launches scripts with an interpreter, so the contract is
    "has a shebang and compiles".

    The POSIX executable bit is not asserted here: Windows cannot preserve it and the
    executor does not depend on it.
    """
    scripts = sorted((SKILLS_ROOT / skill / "scripts").glob("*.py"))
    assert scripts, f"{skill}: no scripts/*.py"
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!"), f"{path} has no shebang"
        compile(text, str(path), "exec")


# ══════════════════════════════════════════════════════════════
# preload: DataLoad naming and manifest must match server-side manifests
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def preload():
    return _load("fluid-cache-preload", "preload.py")


def test_preload_name_is_stable_and_dns_safe(preload):
    # The same --operation-id must always map to the same DataLoad name (a re-run
    # hits 409), and the name must stay bounded and DNS-compatible.
    first = preload.load_name("op-001")
    assert first == preload.load_name("op-001")
    assert first != preload.load_name("op-002")
    assert first.startswith("sinapis-")
    assert len(first) <= 63
    assert all(ch.isalnum() or ch == "-" for ch in first)


def test_preload_body_shape(preload):
    body = preload.load_body("ns", "ds", ["/a", "/b"], 2, "op-001")

    assert body["apiVersion"] == "data.fluid.io/v1alpha1"
    assert body["kind"] == "DataLoad"
    assert body["metadata"]["labels"]["sinapis.fluid.io/operation-id"] == "op-001"
    assert body["spec"]["dataset"] == {"name": "ds", "namespace": "ns"}
    assert body["spec"]["target"] == [{"path": "/a", "replicas": 2},
                                      {"path": "/b", "replicas": 2}]


def test_preload_extract_phase(preload):
    assert preload.extract_phase({"status": {"phase": "Loading"}}) == "Loading"
    assert preload.extract_phase({}) == ""


# ══════════════════════════════════════════════════════════════
# scale_up: target resolution / shrink protection / ready worker reading
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def scale_up():
    return _load("fluid-cache-scale-up", "scale_up.py")


def test_scale_up_resolve_desired(scale_up):
    assert scale_up.resolve_desired(3, None, None) == 4      # default +1
    assert scale_up.resolve_desired(3, 2, None) == 5         # increment
    assert scale_up.resolve_desired(3, None, 10) == 10       # absolute target


def test_scale_up_shrink_is_refused_by_default(scale_up):
    with pytest.raises(ValueError, match="allow-shrink"):
        scale_up.check_scale_direction(4, 3, allow_shrink=False)
    scale_up.check_scale_direction(4, 3, allow_shrink=True)   # explicit permission, no error
    scale_up.check_scale_direction(3, 3, allow_shrink=False)  # unchanged is not a shrink


def test_scale_up_ready_workers_fallbacks(scale_up):
    assert scale_up.ready_workers({"workerNumberReady": 2}) == 2
    status = {"conditions": [{"type": "Capacity", "workerNumberReady": 1},
                             {"type": "Ready", "workerNumberReady": 3}]}
    assert scale_up.ready_workers(status) == 3
    assert scale_up.ready_workers({}) == 0


class _FakeApi:
    """Returns canned ready counts in order; entries may be exceptions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get_namespaced_custom_object(self, *_args, **_kwargs):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else self.responses
        if isinstance(item, Exception):
            raise item
        return {"status": {"workerNumberReady": item}}


def test_scale_up_wait_ready_follows_the_direction(scale_up):
    # Scaling down has to wait for the removed replica to disappear: 2 >= 1 would have
    # returned on the very first poll while the old worker was still ready.
    api = _FakeApi([2, 2, 1])
    assert scale_up.wait_ready(api, "alluxioruntimes", "ns", "rt", 2, 1, 5, 0) == (1, False)
    assert api.calls == 3

    # Scaling up waits for the new replica to become ready.
    api = _FakeApi([1, 2])
    assert scale_up.wait_ready(api, "alluxioruntimes", "ns", "rt", 1, 2, 5, 0) == (2, False)


def test_scale_up_wait_ready_retries_transient_api_errors(scale_up):
    """A single API error must not abort the wait; polling continues to the deadline."""
    from kubernetes.client.exceptions import ApiException

    api = _FakeApi([ApiException(status=500, reason="boom"),
                    ApiException(status=503, reason="unavailable"), 2])
    assert scale_up.wait_ready(api, "alluxioruntimes", "ns", "rt", 1, 2, 5, 0) == (2, False)
    assert api.calls == 3

    # Errors until the deadline report the last known count and a timeout.
    api = _FakeApi([ApiException(status=500, reason="boom")])
    assert scale_up.wait_ready(api, "alluxioruntimes", "ns", "rt", 1, 2, 0, 0) == (1, True)


# ══════════════════════════════════════════════════════════════
# evict_lru: ls/stat parsing and LRU selection
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def evict():
    return _load("fluid-cache-evict-lru", "evict_lru.py")


LS_SAMPLE = """\
Found 4 items
      134217728       PERSISTED 10-26-2026 09:51:00:304 100% /nju-model/cached.bin
      10485760        PERSISTED 10-13-2026 10:16:11:713  40% /nju-model/partial.bin
          512         PERSISTED 10-13-2026 10:16:12:158   0% /nju-model/uncached.bin
             60       PERSISTED 10-13-2026 10:16:26:421  DIR /nju-model/weights
-rwxrwxrwx  root  root       1033  100%   100% /nju-model/legacy-format.bin
2025-01-01 00:00:00,000 INFO fs.AlluxioFileSystem - (PID 1) ...
"""


def test_evict_parse_ls_keeps_only_fully_cached_files(evict):
    candidates = evict.parse_ls(LS_SAMPLE, cached_only=True)
    # Both the modern and legacy ls formats are recognised; only 100%-cached files are kept
    assert candidates == [{"path": "/nju-model/cached.bin", "size_bytes": 134217728},
                          {"path": "/nju-model/legacy-format.bin", "size_bytes": 1033}]

    everything = evict.parse_ls(LS_SAMPLE, cached_only=False)
    assert {f["path"] for f in everything} == {
        "/nju-model/cached.bin", "/nju-model/partial.bin", "/nju-model/uncached.bin",
        "/nju-model/legacy-format.bin",
    }


def test_evict_parse_stat_normalizes_and_degrades(evict):
    stat_text = ("URIStatus{info=FileInfo{path=/a.bin, length=128, "
                 "lastModificationTimesMs=1793008260304, "
                 "lastAccessTimesMs=1791886571713, folder=false}}")
    assert evict.parse_stat(stat_text) == 1791886571713 / 1000  # prefers the last access time

    modify_only = "FileInfo{lastModificationTimesMs=1793008260304, folder=false}"
    # falls back to the modification time
    assert evict.parse_stat(modify_only) == 1793008260304 / 1000
    assert evict.parse_stat("Modification Time: 2025-01-01-00:00:00.000+08:00") is None


def test_evict_pick_victims_lru_order_and_target(evict):
    files = [
        {"path": "/a", "size_bytes": 10, "mtime": 200.0},
        {"path": "/b", "size_bytes": 20, "mtime": 100.0},
        {"path": "/c", "size_bytes": 50, "mtime": None},   # no timestamp ranks last
        {"path": "/d", "size_bytes": 40, "mtime": 150.0},
    ]
    paths = lambda vs: [v["path"] for v in vs]  # noqa: E731

    assert paths(evict.pick_victims(files, target_bytes=60)) == ["/b", "/d"]
    # stops once the total is met
    assert paths(evict.pick_victims(files, target_bytes=5)) == ["/b"]
    # a single oversized file still gets picked
    assert paths(evict.pick_victims([{"path": "/x", "size_bytes": 100, "mtime": 1.0}],
                                    target_bytes=10)) == ["/x"]
    assert paths(evict.pick_victims(files, count=2)) == ["/b", "/d"]
    assert paths(evict.pick_victims(files, count=4)) == ["/b", "/d", "/a", "/c"]


def test_evict_build_results_carries_exit_ok_and_marks_freed(evict):
    """A non-zero free exit must not blow up the error list, and a file whose cached
    ratio dropped counts as freed even when the command complained."""
    submitted = [
        {"path": "/a", "exit_ok": False, "message": "exit 255"},   # freed despite the noise
        {"path": "/b", "exit_ok": False, "message": "exit 255"},   # still cached: a real failure
        {"path": "/c", "exit_ok": True, "message": ""},            # vanished from the listing
    ]
    after = {"/a": 0, "/b": 100}

    results = evict.build_results(submitted, after)
    assert [r["freed"] for r in results] == [True, False, False]
    assert results[0]["exit_ok"] is False and results[2]["cached_after_pct"] is None
    assert evict.failure_messages(results) == ["free /b failed: exit 255"]


def test_evict_master_pod_selectors_fallbacks(evict):
    # Fluid versions label the master Pod differently, so selectors are tried in order
    # (on this cluster the master only has dataset-id)
    assert evict.master_pod_selectors("ns", "ds") == [
        "fluid.io/dataset=ns-ds,role=alluxio-master",
        "fluid.io/dataset-id=ns-ds,role=alluxio-master",
        "app.kubernetes.io/instance=ds,role=alluxio-master",
    ]

    with pytest.raises(SystemExit):
        # exactly one of the two targets is required
        evict.parse_args(["--namespace", "ns", "--dataset", "ds"])
