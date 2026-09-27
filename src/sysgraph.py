"""Phase 57: system dependency graph.

Supervisor -> Runtime -> Workers -> Tools -> Browser -> Database ->
Network -> External Services, plus task-level dependencies.

Before restarting or repairing a component, determine which components
depend on it and restart the SMALLEST safe scope. Never restart the whole
system when a single worker can recover independently.
"""

# Static component graph: component -> components it directly depends on.
GRAPH = {
    "supervisor": [],
    "runtime": ["supervisor"],
    "workers": ["runtime"],
    "tools": ["workers", "runtime"],
    "browser": ["workers", "network"],
    "database": ["runtime"],
    "network": [],
    "external": ["network"],
    "watchdog": ["supervisor"],
    "recovery": ["supervisor", "database"],
}

# Restarting a component requires restarting everything that depends on it.
# This table gives the smallest safe scope per failure.
RESTART_SCOPE = {
    "supervisor": ["supervisor", "watchdog", "runtime", "workers"],
    "watchdog": ["watchdog"],
    "runtime": ["runtime", "workers"],
    "workers": ["workers"],
    "tools": ["tools"],          # tool executor is stateless per call
    "browser": ["browser"],      # browser subsystem restarts alone
    "database": ["database", "runtime"],  # runtime holds the DB handle
    "network": ["network"],      # network is environmental; wait + recheck
    "external": ["external"],    # external: reconcile, don't restart
    "recovery": ["recovery"],
}


def dependents(component):
    """Components that (transitively) depend on this one."""
    out = set()

    def walk(c):
        for comp, deps in GRAPH.items():
            if c in deps and comp not in out:
                out.add(comp)
                walk(comp)
    walk(component)
    return sorted(out)


def restart_scope(failed_component):
    """Smallest safe restart scope for a failed component."""
    return list(RESTART_SCOPE.get(failed_component, [failed_component]))


def blast_radius(component):
    """Everything affected if this component goes down."""
    return sorted(set([component]) | set(dependents(component)))


def task_dependencies_satisfied(store, task_id):
    """Task-level deps: a task's spec may declare depends_on=[task_ids].
    All must be COMPLETED before this task runs."""
    task = store.get_task(task_id)
    if not task:
        return False, ["unknown task"]
    try:
        import json
        spec = json.loads(task.get("spec") or "{}")
    except Exception:
        return True, []
    missing = []
    for dep_id in spec.get("depends_on", []):
        dep = store.get_task(dep_id)
        if not dep or dep["status"] != "COMPLETED":
            missing.append(dep_id)
    return (len(missing) == 0), missing


def affected_by(component, store, journal=None):
    """Report what would be impacted by restarting a component — used by
    diagnostics and dry-run so restarts are never blind."""
    scope = restart_scope(component)
    radius = blast_radius(component)
    running = [t["task_id"] for t in store.list_tasks(status="RUNNING")]
    report = {"component": component, "restart_scope": scope,
              "blast_radius": radius, "running_tasks": running}
    if journal:
        journal("RESTART_SCOPE_COMPUTED", **report)
    return report


def smallest_restart_scope(failed_component):
    """Alias: the smallest safe scope (never the whole system when a
    single component can recover independently)."""
    return restart_scope(failed_component)


def safe_restart_order(components):
    """Order components so dependencies restart before their dependents."""
    order, remaining = [], set(components)
    while remaining:
        progressed = False
        for c in sorted(remaining):
            deps = set(GRAPH.get(c, ())) & set(components)
            if deps <= set(order):
                order.append(c)
                remaining.discard(c)
                progressed = True
        if not progressed:  # cycle or unknown: append the rest deterministically
            order.extend(sorted(remaining))
            break
    return order


def component_health(alive):
    """Map component -> 'ok'|'degraded' from a {component: bool} map."""
    return {c: ("ok" if up else "degraded") for c, up in alive.items()}
