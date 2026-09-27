"""Phase 59: capability-based permissions.

No task gets unrestricted access. Capabilities are granted per task by the
scheduler (or a policy-authorized granter) and stored in the DB. The tool
executor consults this module before running anything.

Capabilities:
  FILES_READ, FILES_WRITE, SHELL_EXECUTE, PROCESS_CONTROL,
  NETWORK_ACCESS, BROWSER_CONTROL, DATABASE_READ, DATABASE_WRITE,
  DEPLOY, SYSTEM_SERVICE_CONTROL

The LLM/model can never grant itself capabilities. Escalation requires
explicit policy authorization (out-of-band token, same boundary as
protected config writes).
"""
import time

CAPABILITIES = {
    "FILES_READ",
    "FILES_WRITE",
    "SHELL_EXECUTE",
    "PROCESS_CONTROL",
    "NETWORK_ACCESS",
    "BROWSER_CONTROL",
    "DATABASE_READ",
    "DATABASE_WRITE",
    "DEPLOY",
    "SYSTEM_SERVICE_CONTROL",
}

# tool -> capability required to run it
TOOL_CAPABILITY = {
    "shell": "SHELL_EXECUTE",
    "write_file": "FILES_WRITE",
    "read_file": "FILES_READ",
    "mkdir": "FILES_WRITE",
    "http_get": "NETWORK_ACCESS",
    "browser": "BROWSER_CONTROL",
}

# Default capability sets per task kind. The scheduler grants only these;
# anything more needs escalation.
DEFAULT_CAPS = {
    "build": {"FILES_READ", "FILES_WRITE", "SHELL_EXECUTE", "NETWORK_ACCESS"},
    "test": {"FILES_READ", "SHELL_EXECUTE"},
    "deploy": {"FILES_READ", "NETWORK_ACCESS", "DEPLOY"},
    "browse": {"NETWORK_ACCESS", "BROWSER_CONTROL", "FILES_READ"},
    "db": {"DATABASE_READ", "DATABASE_WRITE"},
    "admin": set(),  # admin tasks declare their caps explicitly
}

# Capabilities that can never be granted without an auth token.
ESCALATION_CAPS = {
    "PROCESS_CONTROL",
    "SYSTEM_SERVICE_CONTROL",
    "DEPLOY",
    "DATABASE_WRITE",
}


def check(store, task_id, capability):
    """Returns (allowed, reason)."""
    if capability not in CAPABILITIES:
        return False, f"unknown capability: {capability}"
    if store.capability_has(task_id, capability):
        return True, "granted"
    return False, f"capability denied: {capability}"


def check_tool(store, task_id, tool):
    need = TOOL_CAPABILITY.get(tool)
    if need is None:
        return False, f"unknown tool: {tool}"
    return check(store, task_id, need)


def grant(store, task_id, tool, ttl_s=None, granted_by="scheduler"):
    """Grant the capability a tool needs, with an optional TTL (seconds).
    ttl_s=0 means already expired."""
    need = TOOL_CAPABILITY.get(tool)
    if need is None:
        return False, f"unknown tool: {tool}"
    store.capability_grant(task_id, need, granted_by=granted_by,
                           ttl_s=ttl_s)
    return True, f"granted {need} for {tool}"


def grant_all_basic(store, task_id, tools, ttl_s=None, journal=None):
    """Grant exactly the tools in `tools` — the task's spec-declared set.
    Anything not in this set is denied at execution time."""
    granted = []
    for tool in sorted(tools):
        ok, _ = grant(store, task_id, tool, ttl_s=ttl_s)
        if ok:
            granted.append(tool)
    if journal:
        journal("CAPS_GRANTED", task_id=task_id, tools=granted)
    return granted


def revoke(store, task_id, tool, journal=None):
    """Revoke the capability a tool needs."""
    need = TOOL_CAPABILITY.get(tool)
    if need is None:
        return False, f"unknown tool: {tool}"
    store.capability_revoke(task_id, need)
    if journal:
        journal("CAP_REVOKED", task_id=task_id, tool=tool)
    return True, f"revoked {need} for {tool}"


def request_escalation(store, task_id, tool, auth_token=None, journal=None):
    """Escalate to a tool whose capability is escalation-gated.
    Requires an out-of-band auth token; the model can never self-escalate."""
    need = TOOL_CAPABILITY.get(tool)
    if need is None:
        return False, f"unknown tool: {tool}"
    return escalate(store, task_id, need, auth_token=auth_token,
                    journal=journal)


def grant_defaults(store, task_id, task_kind="build", journal=None):
    """Scheduler grants the minimal default set for a task kind."""
    granted = set()
    for cap in DEFAULT_CAPS.get(task_kind, set()):
        store.capability_grant(task_id, cap, granted_by="scheduler")
        granted.add(cap)
    if journal:
        journal("CAPS_GRANTED", task_id=task_id, caps=sorted(granted),
                by="scheduler")
    return granted


def escalate(store, task_id, capability, auth_token=None, journal=None):
    """Capability escalation: requires explicit policy authorization.
    The model can never do this on its own."""
    if capability not in CAPABILITIES:
        return False, f"unknown capability: {capability}"
    if auth_token is None:
        if journal:
            journal("CAP_ESCALATION_REFUSED", task_id=task_id,
                    capability=capability, reason="no auth token")
        return False, (f"capability escalation refused (no auth token): "
                       f"{capability}")
    store.capability_grant(task_id, capability, granted_by="policy:human")
    if journal:
        journal("CAP_ESCALATED", task_id=task_id, capability=capability,
                by="policy:human")
    return True, "escalated with policy authorization"


def revoke_all(store, task_id, journal=None):
    for cap in store.capabilities_for(task_id):
        store.capability_revoke(task_id, cap)
    if journal:
        journal("CAPS_REVOKED", task_id=task_id)
