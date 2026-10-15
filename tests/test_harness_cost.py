"""pi's own model call counts toward the task's cost; absent usage falls back to a flagged estimate."""

from app.harness.pi_runner import estimate_usage, usage_of
from app.harness.tool_server import Visit


def test_usage_is_read_from_several_shapes():
    assert usage_of({"usage": {"input": 10, "output": 5}}) == (10, 5)
    assert usage_of({"message": {"usage": {"input_tokens": 7, "output_tokens": 3}}}) == (7, 3)
    assert usage_of({"data": {"usage": {"inputTokens": 2, "outputTokens": 1}}}) == (2, 1)


def test_missing_or_bad_usage_is_zero():
    assert usage_of({}) == (0, 0)
    assert usage_of({"usage": {"input": "lots", "output": -4}}) == (0, 0)
    assert usage_of({"message": "text"}) == (0, 0)
    assert usage_of({"usage": {"input": True}}) == (0, 0)


def test_estimate_is_positive_and_grows_with_the_pages_read():
    small = estimate_usage("q", [], "a")
    v = Visit(url="https://a.com", title="t", text="x" * 4000)
    assert small[0] >= 1 and small[1] >= 1
    assert estimate_usage("q", [v], "a")[0] > small[0]
