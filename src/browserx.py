"""Phase 44: browser recovery subsystem.

The browser is a separate subsystem with its own lifecycle. The runtime
never assumes the browser is healthy because a process exists — it verifies
session validity (driver responds) before trusting it.

States: DOWN | STARTING | HEALTHY | DEGRADED | RECOVERING
Recovery: detect -> capture state -> restart if appropriate -> restore
session where possible -> verify expected page/state -> resume.
"""
import time


class BrowserSubsystem:
    def __init__(self, store, journal=None, driver=None):
        self.store = store
        self.journal = journal or store.journal
        self.driver = driver  # host-provided driver or None
        self.state = "DOWN"
        self.last_check = 0
        self.fail_count = 0

    # ---- health ----
    def check_health(self):
        """Verify the browser actually works, not just that a pid exists."""
        self.last_check = time.time()
        if self.driver is None:
            self.state = "DOWN"
            return {"state": "DOWN", "reason": "no host driver wired in"}
        try:
            ok = self.driver.ping(timeout=10)
        except Exception as e:  # noqa: BLE001
            ok = False
            self.journal("BROWSER_PING_FAILED", error=repr(e))
        if ok:
            self.state = "HEALTHY"
            self.fail_count = 0
        else:
            self.fail_count += 1
            self.state = "DEGRADED" if self.fail_count < 3 else "DOWN"
        self.store.world_set("browser.state", self.state,
                             verifier="browser_subsystem")
        return {"state": self.state, "fail_count": self.fail_count}

    def classify_failure(self, error_text):
        t = str(error_text or "").lower()
        if "crash" in t or "renderer" in t:
            return "browser_crash"
        if "session" in t and ("invalid" in t or "expired" in t
                               or "disconnected" in t):
            return "session_invalid"
        if "timeout" in t:
            return "page_timeout"
        if "navigation" in t or "net::" in t:
            return "navigation_failure"
        if "driver" in t:
            return "driver_failure"
        if "auth" in t or "login" in t or "401" in t:
            return "auth_expired"
        return "unknown"

    # ---- recovery ----
    def recover(self, failure_kind=None, expected_state=None):
        """Restart browser if appropriate, restore session, verify."""
        self.journal("BROWSER_RECOVERY_STARTED", failure_kind=failure_kind)
        self.state = "RECOVERING"
        if self.driver is None:
            self.journal("BROWSER_RECOVERY_SKIPPED",
                         reason="no host driver; browser unavailable")
            self.state = "DOWN"
            return {"recovered": False,
                    "reason": "no host driver wired in"}

        if failure_kind in ("browser_crash", "driver_failure",
                            "session_invalid", None):
            try:
                self.driver.restart()
            except Exception as e:  # noqa: BLE001
                self.journal("BROWSER_RESTART_FAILED", error=repr(e))
                self.state = "DOWN"
                return {"recovered": False, "error": repr(e)}

        # restore session where possible
        restored = False
        try:
            restored = bool(self.driver.restore_session())
        except Exception:  # noqa: BLE001
            pass

        health = self.check_health()
        verified = health["state"] == "HEALTHY"
        if verified and expected_state:
            try:
                verified = bool(self.driver.verify_state(expected_state))
            except Exception:  # noqa: BLE001
                verified = False
        self.journal("BROWSER_RECOVERY_DONE", recovered=verified,
                     session_restored=restored)
        return {"recovered": verified, "session_restored": restored,
                "health": health}
