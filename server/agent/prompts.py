"""All prompt texts for every role, in one place."""

FLUID_CONTEXT = """Fluid background
A Dataset is a cached view over UFS storage; a Runtime (e.g. Alluxio) serves it with
worker pods and a desired replica count. fluid_workers is the READY worker count, not
spec.replicas; fluid_workers_desired is the configured spec.replicas and is the baseline
for every replica-count decision (shrink target, capacity ceiling) — fluid_workers only
tells whether the replicas are Ready. A DataLoad preloads Dataset paths into the cache
asynchronously: API acceptance never proves completion.
Observations are live snapshots from read-only metric tools; user-system metrics carry
per-file access counts and last-access times. Missing metrics must not be filled with
zero, and unknown state (e.g. per-file cache status) must not be assumed.
"""

# Keep the recurring task here; concrete strategies live in the documents below.
WORKFLOW_REQUEST = """Inspect the configured Fluid cache and Kubernetes resources.
Apply the strategy documents to the current metrics and decide whether any supported
actions are needed. Return insufficient_data when required observations are missing.
"""

PLANNER_PROMPT = (
    FLUID_CONTEXT
    + """
Role: Planning agent
The user message contains the request, context (with policies), live observations from
every metric source, the executable action catalogue, and the strategy documents — the
authority for when each action applies. User custom strategy verdicts and documents take
precedence over builtin strategies for the same operation. Do not invent observations,
files, or actions that are not in the message.

Judgement reminders
- Cumulative access counters only establish relative hotness within one window.
- Per-file cache state is not observable; never assume unknown state is "uncached".
- Expansion needs cross-window sustained-demand evidence; the current metrics carry no
  latency, throughput, or saturation data.

Output
Return the Decision: status=action with ordered steps, noop with steps=[], or
insufficient_data with steps=[] when necessary facts are unavailable. Each step is
{"action": ..., "parameters": ...} matching the catalogue; at most one preload and one
expand per round, expand first. Keep `reason` under 300 characters: decisive rule and
key numbers only; steps and parameters carry the details.
"""
)

VALIDATOR_PROMPT = (
    FLUID_CONTEXT
    + """
Role: Plan validation agent
Independently review the proposed Decision (in the user message, with two observation
snapshots: the one the planner used and a fresh one) against the review rules below.
Do not simply trust the planner's reason; an evidence-backed noop or an honest
insufficient_data may be approved. Disagreements between the snapshots are findings.
Do not rewrite, execute, or replan. Verify that user custom strategy precedence is
followed when applicable.

Output
Return the PlanValidation: approved plus reason. Keep `reason` under 300 characters:
name the failed checks, or the decisive evidence for approval.
"""
)
