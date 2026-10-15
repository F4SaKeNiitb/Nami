"""Runs the scripted end-to-end checks under pytest so CI fails when any one of them fails.

These scripts (orchestration with a scripted model, authz matrix, identity tokens) are slower integration checks.
Each runs once per session in a subprocess; every PASS/FAIL line becomes its own assertion.
"""

import os
import re
import subprocess
import sys

import pytest

SCRIPTS = ["smoke_graph", "smoke_authz", "smoke_identity"]
_LINE = re.compile(r"^\s*(PASS|FAIL)\s+(.*)$")


@pytest.fixture(scope="module")
def outputs():
    res = {}
    for name in SCRIPTS:
        p = subprocess.run([sys.executable, f"scripts/{name}.py"], capture_output=True, text=True, timeout=600,
                           env={**os.environ, "PYTHONPATH": "."})
        res[name] = (p.returncode, p.stdout, p.stderr)
    return res


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exits_zero_and_has_no_failed_check(outputs, name):
    code, out, err = outputs[name]
    failed = [m.group(2) for line in out.splitlines() if (m := _LINE.match(line)) and m.group(1) == "FAIL"]
    assert not failed, "failed checks:\n" + "\n".join(failed)
    assert code == 0, err[-2000:]


@pytest.mark.parametrize("name,minimum", [("smoke_graph", 60), ("smoke_identity", 25)])
def test_script_actually_ran_its_checks(outputs, name, minimum):
    passed = sum(1 for line in outputs[name][1].splitlines() if (m := _LINE.match(line)) and m.group(1) == "PASS")
    assert passed >= minimum, f"only {passed} checks reported by {name}"
