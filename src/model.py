"""Phase 36/37/38: model adapter — failure recovery, output validation,
context recovery.

The model proposes; the runtime disposes. This module:
  - validates every model-generated action against a strict schema (38)
  - retries transient model failures, falls back where configured (37)
  - saves a recovery summary and reloads verified state when context
    approaches its limit, so a task survives context exhaustion (36)

The fallback model receives verified task state from SQLite, never relies
on missing conversational context.
"""
import json
import time

# ---- Phase 38: strict action schema ----
ACTION_SCHEMA = {
    "type": "object",
    "required": ["tool", "args"],
    "properties": {
        "tool": {"type": "string",
                 "enum": ["shell", "write_file", "read_file", "mkdir",
                          "http_get", "browser"]},
        "args": {"type": "object"},
        "op_id": {"type": "string"},
        "verify": {"type": "array"},
    },
}

REQUIRED_ARG_FIELDS = {
    "shell": ["command"],
    "write_file": ["path", "content"],
    "read_file": ["path"],
    "mkdir": ["path"],
    "http_get": ["url"],
    "browser": [],
}


def validate_action(action):
    """Returns (ok, errors). Never executes; only validates shape."""
    errors = []
    if not isinstance(action, dict):
        return False, ["action must be an object"]
    for field in ACTION_SCHEMA["required"]:
        if field not in action:
            errors.append(f"missing required field: {field}")
    tool = action.get("tool")
    if tool not in ACTION_SCHEMA["properties"]["tool"]["enum"]:
        errors.append(f"unknown tool: {tool}")
    else:
        args = action.get("args") or {}
        if not isinstance(args, dict):
            errors.append("args must be an object")
        else:
            for req in REQUIRED_ARG_FIELDS.get(tool, []):
                if req not in args:
                    errors.append(f"tool {tool}: missing arg '{req}'")
    if "verify" in action and not isinstance(action["verify"], list):
        errors.append("verify must be a list")
    return (len(errors) == 0), errors


# ---- Phase 37: model failure classification + retry/fallback ----
MODEL_FAILURE_POLICIES = {
    "timeout": (True, 3, 5),
    "rate_limit": (True, 5, 10),
    "auth_failure": (False, 0, 0),   # needs human, never blind-retry
    "malformed_response": (True, 2, 2),
    "model_unavailable": (True, 4, 10),
    "context_limit": (False, 0, 0),  # handled by context recovery instead
    "inference_failure": (True, 2, 5),
}


def classify_model_failure(error_text):
    t = str(error_text or "").lower()
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "rate" in t and "limit" in t or "429" in t:
        return "rate_limit"
    if "auth" in t or "401" in t or "403" in t or "api key" in t:
        return "auth_failure"
    if "json" in t or "malformed" in t or "parse" in t:
        return "malformed_response"
    if "unavailable" in t or "overloaded" in t or "503" in t:
        return "model_unavailable"
    if "context" in t and ("limit" in t or "length" in t or "token" in t):
        return "context_limit"
    return "inference_failure"


class ModelAdapter:
    """Wraps model calls with retry/fallback. `primary` and optional
    `fallback` are callables: fn(prompt, state) -> dict action."""

    def __init__(self, store, primary, fallback=None, journal=None):
        self.store = store
        self.primary = primary
        self.fallback = fallback
        self.journal = journal or store.journal

    def _verified_state(self, task_id):
        task = self.store.get_task(task_id) or {}
        ckpt = self.store.latest_checkpoint(task_id)
        return {"task": {k: task.get(k) for k in
                         ("task_id", "status", "current_step",
                          "completed_steps", "failed_steps")},
                "checkpoint": ckpt["label"] if ckpt else None,
                "world": self.store.world_all()}

    def propose(self, task_id, prompt, context_tokens=0, context_limit=0):
        """Get a validated action proposal, surviving model failures."""
        # Phase 36: context approaching limit -> recover first
        if context_limit and context_tokens >= 0.85 * context_limit:
            summary = self.context_recover(task_id, prompt)
            prompt = ("[context recovered] verified state:\n"
                      + json.dumps(summary)[:4000] + "\n\n" + prompt)

        attempt_fns = [("primary", self.primary)]
        if self.fallback:
            attempt_fns.append(("fallback", self.fallback))
        last_err = "no model call attempted"
        for name, fn in attempt_fns:
            retryable, max_retries, base = (True, 2, 2)
            for attempt in range(max_retries + 1):
                try:
                    action = fn(prompt, self._verified_state(task_id))
                except Exception as e:  # noqa: BLE001
                    kind = classify_model_failure(str(e))
                    retryable, mr, b = MODEL_FAILURE_POLICIES[kind]
                    last_err = f"{name} {kind}: {e}"
                    self.journal("MODEL_FAILURE", task_id=task_id,
                                 model=name, kind=kind, attempt=attempt)
                    if not retryable or attempt >= mr:
                        break
                    time.sleep(min(b * (2 ** attempt), 60))
                    continue
                ok, errors = validate_action(action)
                if not ok:
                    last_err = f"{name} invalid action: {errors}"
                    self.journal("MODEL_INVALID_ACTION", task_id=task_id,
                                 model=name, errors=errors)
                    break  # don't retry malformed output blindly
                self.store.claim(task_id, {"model": name, "action": action})
                return action
        # every model path failed: safe recovery state, task preserved
        self.journal("MODEL_ALL_FAILED", task_id=task_id, last_err=last_err)
        self.store.update_task(task_id, status="PAUSED")
        self.store.intervention_open(
            task_id=task_id, reason=f"model failure: {last_err}",
            required_action="check model credentials/quota, then resume",
            last_verified_step="model propose")
        return None

    # ---- Phase 36: context recovery ----
    def context_recover(self, task_id, current_objective=""):
        """Extract verified state into a recovery summary; the DB stays
        authoritative. Returns the summary dict (also journaled)."""
        task = self.store.get_task(task_id) or {}
        ckpt = self.store.latest_checkpoint(task_id)
        summary = {
            "task_id": task_id,
            "objective": current_objective,
            "status": task.get("status"),
            "current_step": task.get("current_step"),
            "completed_steps": task.get("completed_steps"),
            "failed_steps": task.get("failed_steps"),
            "last_checkpoint": ckpt["label"] if ckpt else None,
            "last_verified_result": task.get("last_verified_result"),
            "world": self.store.world_all(),
            "recovered_at": time.time(),
        }
        self.store.snapshot(task_id, "context-recovery", summary)
        self.journal("CONTEXT_RECOVERED", task_id=task_id,
                     checkpoint=summary["last_checkpoint"])
        return summary
