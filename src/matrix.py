"""Phase 79: recovery matrix as runtime policy.

For every important failure: FAILURE -> DETECTION -> CLASSIFICATION ->
RECOVERY -> VERIFICATION -> RETRY LIMIT -> ESCALATION.

This is not documentation: RecoveryManager consults `policy_for()` at
runtime to pick classification, recovery action, verification, retry
limits, and escalation targets. The static table here is the single
source; classify.py provides the deterministic retry numbers.
"""
from . import classify as classify_mod

# failure_kind -> matrix entry
MATRIX = {
    "agent_crash": {
        "detection": "supervisor: process exit / missing heartbeat",
        "classification": ("process", "crash"),
        "recovery": "restart_agent",
        "verification": "heartbeat fresh + task resumed",
        "retry_limit": 12,  # per hour, then refuse
        "escalation": "safe_mode",
    },
    "agent_hang": {
        "detection": "supervisor: stale heartbeat + no progress",
        "classification": ("process", "hang"),
        "recovery": "restart_agent",
        "verification": "heartbeat fresh + progress resumes",
        "retry_limit": 5,
        "escalation": "safe_mode",
    },
    "worker_crash": {
        "detection": "executor: subprocess died unexpectedly",
        "classification": ("tool", "crash"),
        "recovery": "retry_step",
        "verification": "verifier PASS on retried step",
        "retry_limit": 2,
        "escalation": "reload_task_state",
    },
    "step_failed": {
        "detection": "verifier: FAIL verdict",
        "classification": ("tool", "exit_nonzero"),
        "recovery": "retry_step",
        "verification": "verifier PASS",
        "retry_limit": 1,
        "escalation": "rebuild_plan",
    },
    "browser_crash": {
        "detection": "browser subsystem: ping failed",
        "classification": ("browser", "browser_crash"),
        "recovery": "restart_browser",
        "verification": "browser ping HEALTHY + session valid",
        "retry_limit": 3,
        "escalation": "safe_mode",
    },
    "network_failure": {
        "detection": "net classifier on tool stderr",
        "classification": ("net", "connection_timeout"),
        "recovery": "backoff_retry",
        "verification": "operation eventually succeeds",
        "retry_limit": 5,
        "escalation": "human",
    },
    "dns_failure": {
        "detection": "net classifier: name resolution",
        "classification": ("net", "dns_failure"),
        "recovery": "backoff_retry",
        "verification": "DNS resolves",
        "retry_limit": 5,
        "escalation": "human",
    },
    "auth_failure": {
        "detection": "net/model classifier: 401/403/invalid credentials",
        "classification": ("net", "auth_failure"),
        "recovery": "none — open intervention",
        "verification": "human provides valid credential",
        "retry_limit": 0,
        "escalation": "human",
    },
    "model_failure": {
        "detection": "model adapter: exception on propose()",
        "classification": ("model", "inference_failure"),
        "recovery": "retry_model",
        "verification": "validated action returned",
        "retry_limit": 2,
        "escalation": "fallback_model",
    },
    "model_malformed": {
        "detection": "model adapter: schema validation failed",
        "classification": ("model", "malformed_response"),
        "recovery": "retry_model",
        "verification": "action validates",
        "retry_limit": 2,
        "escalation": "human",
    },
    "planner_stall": {
        "detection": "planner: repetitive/circular/impossible plan",
        "classification": ("planner", "stall"),
        "recovery": "rebuild_plan",
        "verification": "new plan validates + progress resumes",
        "retry_limit": 2,
        "escalation": "human",
    },
    "progress_stall": {
        "detection": "progress tracker: no observable change",
        "classification": ("progress", "stall"),
        "recovery": "retry_step",
        "verification": "fingerprint diff non-empty",
        "retry_limit": 3,
        "escalation": "rebuild_plan",
    },
    "deadlock": {
        "detection": "wait-graph cycle",
        "classification": ("lock", "deadlock"),
        "recovery": "release_stale",
        "verification": "no cycle in wait graph",
        "retry_limit": 2,
        "escalation": "restart_worker",
    },
    "stale_lock": {
        "detection": "lock lease expired + owner dead",
        "classification": ("lock", "stale"),
        "recovery": "reap_lock",
        "verification": "lock released, task resumed",
        "retry_limit": 1,
        "escalation": "human",
    },
    "dep_failed": {
        "detection": "deps check: check returned not-ok",
        "classification": ("dep", "missing"),
        "recovery": "repair_dep",
        "verification": "dep check passes after repair",
        "retry_limit": 2,
        "escalation": "human",
    },
    "dep_permanent": {
        "detection": "deps: repair unsafe or not possible",
        "classification": ("dep", "version_mismatch"),
        "recovery": "none — open intervention",
        "verification": "human resolves",
        "retry_limit": 0,
        "escalation": "human",
    },
    "db_corrupt": {
        "detection": "sqlite integrity_check != ok",
        "classification": ("db", "corrupt"),
        "recovery": "restore_backup",
        "verification": "integrity_check ok + tasks present",
        "retry_limit": 1,
        "escalation": "human",
    },
    "disk_full": {
        "detection": "resources: disk_free_mb below critical",
        "classification": ("resource", "disk"),
        "recovery": "rotate_logs + clean_tmp + pause_low_priority",
        "verification": "disk_free_mb above warning",
        "retry_limit": 3,
        "escalation": "safe_mode",
    },
    "config_bad": {
        "detection": "integrity: config hash mismatch / validation failed",
        "classification": ("config", "bad"),
        "recovery": "restore_config",
        "verification": "config validates + health ok",
        "retry_limit": 1,
        "escalation": "human",
    },
    "budget_exhausted": {
        "detection": "budget_consume returns not-ok",
        "classification": ("budget", "exhausted"),
        "recovery": "pause_task",
        "verification": "human raises budget or approves",
        "retry_limit": 0,
        "escalation": "human",
    },
    "capability_denied": {
        "detection": "caps.check fails for tool",
        "classification": ("policy", "capability_denied"),
        "recovery": "none — request escalation",
        "verification": "policy-authorized grant",
        "retry_limit": 0,
        "escalation": "human",
    },
    "protected_op": {
        "detection": "policy.classify == PROTECTED",
        "classification": ("policy", "protected"),
        "recovery": "none — refused",
        "verification": "auth token presented out-of-band",
        "retry_limit": 0,
        "escalation": "human",
    },
    "repeated_failure": {
        "detection": "fingerprint occurrences exceed threshold",
        "classification": ("failure", "repeated"),
        "recovery": "escalate_strategy",
        "verification": "different strategy attempted",
        "retry_limit": 0,
        "escalation": "human",
    },
}


def policy_for(failure_kind):
    """Return the matrix entry for a failure kind, or the unknown default."""
    entry = MATRIX.get(failure_kind)
    if entry:
        return dict(entry)
    return {
        "detection": "unknown",
        "classification": ("unknown", "unknown"),
        "recovery": "retry_step",
        "verification": "verifier PASS",
        "retry_limit": 2,
        "escalation": "human",
    }


def classify_failure(failure_kind):
    """Deterministic classification for a matrix failure kind."""
    entry = policy_for(failure_kind)
    source, kind = entry["classification"]
    return classify_mod.classify(source, kind)


def recovery_action(failure_kind):
    return policy_for(failure_kind)["recovery"]


def escalation_for(failure_kind):
    return policy_for(failure_kind)["escalation"]


def retry_limit_for(failure_kind):
    return policy_for(failure_kind)["retry_limit"]


def all_kinds():
    return sorted(MATRIX)
