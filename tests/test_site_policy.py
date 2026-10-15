"""Approval policy is derived from the HTTP method; a registry label can raise risk but never lower it."""

import pytest

from app.sites import loader
from app.sites.policy import lint, needs_approval, risk


@pytest.mark.parametrize("spec,expected", [
    ({}, "read"),                                              # no method: GET
    ({"method": "GET"}, "read"),
    ({"method": "head"}, "read"),
    ({"method": "POST"}, "write"),
    ({"method": "PUT"}, "write"),
    ({"method": "PATCH"}, "write"),
    ({"method": "DELETE"}, "destructive"),
    ({"method": "delete"}, "destructive"),
    ({"method": "SOMETHING"}, "write"),                        # unknown methods are treated as changes
])
def test_risk_comes_from_the_method(spec, expected):
    assert risk(spec) == expected


def test_a_read_label_cannot_hide_a_post_or_delete():
    assert risk({"kind": "read", "method": "POST"}) == "write"
    assert risk({"kind": "read", "method": "DELETE"}) == "destructive"


def test_a_generate_label_needs_approval_even_if_the_method_looks_safe():
    assert needs_approval({"kind": "generate", "method": "GET"})
    assert needs_approval({"kind": "generate"})


def test_an_unknown_label_is_not_treated_as_read():
    assert needs_approval({"kind": "mystery"})


def test_plain_reads_need_no_approval():
    assert not needs_approval({"path": "/credits"}) and not needs_approval({"kind": "read", "method": "GET"})


def test_the_shipped_registry_is_consistent():
    assert lint(loader._load()["sites"]) == []


def test_every_shipped_generate_endpoint_needs_approval_and_every_read_does_not():
    for site, d in loader._load()["sites"].items():
        for name, spec in ((d.get("api") or {}).get("endpoints") or {}).items():
            assert needs_approval(spec) == (spec.get("kind") == "generate"), f"{site}.{name}"


def test_lint_flags_inconsistent_declarations():
    sites = {"x.com": {"api": {"endpoints": {
        "a": {"kind": "read", "method": "POST"},
        "b": {"kind": "generate", "method": "GET", "body": {}},
        "c": {"kind": "generate", "method": "POST"},
        "d": {"kind": "weird"},
        "ok": {"path": "/x"},
    }}}}
    bad = lint(sites)
    assert any("x.com.a" in p for p in bad) and any("x.com.b" in p for p in bad)
    assert any("x.com.c" in p for p in bad) and any("x.com.d" in p for p in bad)
    assert not any("x.com.ok" in p for p in bad)
