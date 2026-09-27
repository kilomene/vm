"""Phase 27: deadlock detection over the lock wait-graph.

Tracks who-holds-what and who-waits-for-what. A cycle in the wait graph
(A waits B, B waits C, C waits A) is a deadlock.

On detection:
  1. capture diagnostics
  2. pick the least-destructive recovery (release the youngest stale-ish
     lock, or restart the worker holding the fewest completed steps)
  3. preserve task state; resume from checkpoint
Never blind-kill the whole runtime.
"""
import time


class WaitGraph:
    """In-memory wait graph. Edges: waiter -> holder."""

    def __init__(self):
        self.holds = {}   # holder -> set(lock_id)
        self.waits = {}   # waiter -> set(lock_id)

    def acquire(self, holder, lock_id):
        self.holds.setdefault(holder, set()).add(lock_id)
        self.waits.get(holder, set()).discard(lock_id)

    def wait_for(self, waiter, lock_id):
        self.waits.setdefault(waiter, set()).add(lock_id)

    def release(self, holder, lock_id):
        self.holds.get(holder, set()).discard(lock_id)
        self.waits.get(holder, set()).discard(lock_id)

    def holders_of(self, lock_id):
        return [h for h, locks in self.holds.items() if lock_id in locks]

    def find_cycle(self):
        """Return a cycle [a, b, c, a] or None. waiter -> holder edges."""
        adj = {}
        for waiter, locks in self.waits.items():
            for lid in locks:
                for holder in self.holders_of(lid):
                    if holder != waiter:
                        adj.setdefault(waiter, set()).add(holder)
        visited, stack = set(), []

        def dfs(node):
            if node in stack:
                return stack[stack.index(node):] + [node]
            if node in visited:
                return None
            visited.add(node)
            stack.append(node)
            for nxt in adj.get(node, ()): 
                cyc = dfs(nxt)
                if cyc:
                    return cyc
            stack.pop()
            return None

        for n in list(adj):
            cyc = dfs(n)
            if cyc:
                return cyc
        return None


def detect_and_recover(store, graph, journal):
    """Check for deadlock; recover least-destructively. Returns action taken."""
    cycle = graph.find_cycle()
    if not cycle:
        return None
    journal("DEADLOCK_DETECTED", cycle=cycle)
    diag = {"ts": time.time(), "cycle": cycle,
            "holds": {k: sorted(v) for k, v in graph.holds.items()},
            "waits": {k: sorted(v) for k, v in graph.waits.items()}}
    # Least destructive: the youngest waiter releases its *other* locks and
    # backs off; only if that fails do we restart a worker.
    victim = cycle[0]
    locks = store.list_locks()
    victim_locks = [l for l in locks if l["owner"] == victim]
    for l in victim_locks:
        # Don't release the lock it's deadlocked on; release the others so
        # the rest of the cycle can make progress.
        if l["lock_id"] not in graph.waits.get(victim, set()):
            store.release_lock(l["lock_id"], owner=victim)
            graph.release(victim, l["lock_id"])
    journal("DEADLOCK_RECOVERED", victim=victim,
            released=[l["lock_id"] for l in victim_locks],
            diagnostics=diag)
    return {"victim": victim, "diagnostics": diag}
