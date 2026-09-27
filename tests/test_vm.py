"""vm-agent integration tests. Run against a live install: pytest tests/ -v.

These tests exercise real behavior (process kills, service restarts), not mocks.
They need root and the vm-agent service installed.
"""
import json
import os
import subprocess
import time

import pytest

PREFIX = os.environ.get("VM_AGENT_HOME", "/opt/vm-agent")
CLI = os.path.join(PREFIX, "bin", "vm-agent")
TEST_OUT = os.path.join(PREFIX, "test-out")


def cli(*args):
    r = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=30)
    return r


def health():
    r = cli("health")
    return json.loads(r.stdout)


def submit_task(task_id, steps):
    os.makedirs(TEST_OUT, exist_ok=True)
    spec_path = f"/tmp/vm-test-{task_id}.json"
    with open(spec_path, "w") as f:
        json.dump({"steps": steps}, f)
    r = cli("submit", spec_path, "--id", task_id)
    assert r.returncode == 0, r.stderr
    return task_id


def wait_for(task_id, want=("COMPLETED", "FAILED"), timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = cli("tasks")
        for line in r.stdout.splitlines():
            if line.startswith(task_id + "\t"):
                status = line.split("\t")[1]
                if status in want:
                    return status
        time.sleep(3)
    raise TimeoutError(f"{task_id} did not reach {want}")


def test_supervisor_and_agent_up():
    h = health()
    assert h["supervisor"] == "up"
    assert h["agent"] == "up"
    assert h["heartbeat_age_s"] < 30


def test_task_completes_with_verification():
    tid = "pytest-basic"
    submit_task(tid, [{
        "name": "write file",
        "tool": "write_file",
        "args": {"path": f"{TEST_OUT}/pytest.txt", "content": "ok"},
        "verify": [{"check": "file_contains",
                    "path": f"{TEST_OUT}/pytest.txt", "text": "ok"}],
    }])
    assert wait_for(tid) == "COMPLETED"
    with open(f"{TEST_OUT}/pytest.txt") as f:
        assert f.read() == "ok"


def test_verifier_rejects_bad_result():
    tid = "pytest-verify-fail"
    submit_task(tid, [{
        "name": "bad verify",
        "tool": "shell", "args": {"command": "echo hi"},
        "verify": [{"check": "file_contains",
                    "path": f"{TEST_OUT}/pytest.txt", "text": "ABSENT_XYZ"}],
        "retries": 0,
    }])
    assert wait_for(tid) == "FAILED"


def test_policy_blocks_protected():
    tid = "pytest-policy"
    submit_task(tid, [{
        "name": "protected",
        "tool": "shell", "args": {"command": "reboot"},
        "retries": 0,
    }])
    assert wait_for(tid) == "FAILED"


def test_agent_crash_recovery():
    h0 = health()
    pid0 = h0["agent_pid"]
    os.kill(pid0, 9)  # SIGKILL the agent
    deadline = time.time() + 60
    while time.time() < deadline:
        h = health()
        if h["agent"] == "up" and h["agent_pid"] != pid0:
            break
        time.sleep(3)
    else:
        pytest.fail("agent did not restart after SIGKILL")
    assert h["restarts_last_hour"] >= 1


def test_idempotent_resume():
    tid = "pytest-resume"
    marker = f"{TEST_OUT}/resume-count.txt"
    if os.path.exists(marker):
        os.unlink(marker)
    submit_task(tid, [
        {"name": "s1",
         "tool": "shell",
         "args": {"command": f"echo 1 >> {marker}"},
         "verify": [{"check": "file_exists", "path": marker}],
         "idempotent_check": {"check": "file_exists", "path": marker}},
        {"name": "s2",
         "tool": "shell",
         "args": {"command": "sleep 20", "timeout_s": 30},
         "verify": []},
    ])
    time.sleep(8)  # let s1 finish, s2 start
    h = health()
    os.kill(h["agent_pid"], 9)
    assert wait_for(tid, timeout=120) == "COMPLETED"
    with open(marker) as f:
        lines = f.read().strip().splitlines()
    assert lines == ["1"], f"s1 must run exactly once (idempotent): {lines}"
