"""Phase 34/35: explicit world state + model-claim separation.

world_state: machine-readable verified environment facts. ONLY the verifier
writes here (via world_set). The LLM/agent may read but never write directly.

model_claims: what the model asserted, stored separately with timestamps.
When a claim and an observation disagree, the verified observation wins —
always. `reconcile()` produces the explicit claim-vs-observation record.
"""
import time

from .state import Store


def record_observation(store, key, value, verifier):
    """Verifier-only write path for world state."""
    store.world_set(key, value, verifier)
    store.journal("WORLD_OBSERVED", key=key, value=value, verifier=verifier)


def record_claim(store, task_id, claim):
    """Store what the model asserted, separately from observations."""
    store.claim(task_id, claim)


def reconcile(store, task_id, claim, observations):
    """Compare a model claim against verified observations.

    observations: {key: (observed_value, verifier)}.
    Returns {"verified": bool, "mismatches": [...]} — verified result wins.
    """
    mismatches = []
    for key, (observed, verifier) in observations.items():
        claimed = claim.get(key, "<not claimed>")
        if claimed != observed:
            mismatches.append({"key": key, "claimed": claimed,
                               "observed": observed, "verifier": verifier})
        record_observation(store, key, observed, verifier)
    result = {"verified": not mismatches, "mismatches": mismatches,
              "ts": time.time()}
    store.journal("CLAIM_RECONCILED", task_id=task_id, **result)
    return result


def snapshot_world(store):
    return store.world_all()
