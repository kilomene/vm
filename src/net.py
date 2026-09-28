"""Phase 43: network failure classification + retry policy.

Not all network failures deserve the same response. Auth failures must not
be retried blindly; transient failures get exponential backoff; task state
is preserved while waiting.
"""
import re
import time

# classification -> (retryable, max_retries, base_backoff_s, note)
POLICIES = {
    "dns_failure":       (True, 5, 2, "transient: backoff and retry"),
    "no_internet":       (True, 8, 5, "transient: backoff and retry"),
    "connection_timeout":(True, 5, 2, "transient: backoff and retry"),
    "tls_failure":       (True, 3, 5, "maybe transient (clock/proxy); limited retries"),
    "rate_limited":      (True, 6, 10, "honor backoff; do not hammer"),
    "remote_server_error":(True, 4, 5, "5xx: backoff and retry"),
    "auth_failure":      (False, 0, 0, "never retry blindly: needs human/credential fix"),
    "local_firewall":    (False, 0, 0, "needs human: do not retry"),
    "unknown":           (True, 3, 5, "cautious default"),
}

# Classification patterns. Numeric patterns are HTTP-context or
# word-boundary anchored: bare substrings like "401" misfire on PIDs
# ("process 40123 exited"), byte counts ("wrote 429 bytes"), and version
# numbers, misclassifying permanent local errors as transient network
# ones (which then burn retries and backoff).
_PATTERNS = [
    ("dns_failure", [r"Name or service not known", r"\bDNS\b", r"getaddrinfo failed",
                     r"nodename nor servname", r"Temporary failure in name resolution"]),
    ("no_internet", [r"Network is unreachable", r"No route to host"]),
    ("connection_timeout", [r"timed out", r"Connection timed out", r"TimeoutError"]),
    ("tls_failure", [r"\bSSL\b", r"certificate", r"\bTLS\b", r"handshake failure"]),
    ("rate_limited", [r"\bHTTP(?: Error)? 429\b", r"rate limit", r"Too Many Requests",
                      r"temporarily blocked"]),
    ("remote_server_error", [r"\bHTTP(?: Error)? 5\d\d\b", r"Internal Server Error",
                             r"Bad Gateway", r"Service Unavailable",
                             r"Gateway Timeout"]),
    ("auth_failure", [r"\bHTTP(?: Error)? 40[13]\b", r"Unauthorized", r"Forbidden",
                      r"authentication failed", r"invalid credentials",
                      r"invalid token"]),
    ("local_firewall", [r"Permission denied.*connect", r"firewall", r"blocked by policy"]),
]


def classify(error_text):
    """Classify a network error string into a failure kind."""
    text = str(error_text or "")
    for kind, patterns in _PATTERNS:
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                return kind
    return "unknown"


def backoff_delay(kind, attempt):
    retryable, max_retries, base, _ = POLICIES.get(kind, POLICIES["unknown"])
    if not retryable or attempt >= max_retries:
        return None
    return min(base * (2 ** attempt), 300)


def should_retry(kind, attempt):
    return backoff_delay(kind, attempt) is not None


def describe(kind):
    return POLICIES.get(kind, POLICIES["unknown"])[3]
