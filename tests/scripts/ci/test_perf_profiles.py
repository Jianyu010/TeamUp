import json

from tests.scenarios.perf import __main__ as perf


def test_weekly_profile_records_its_own_results(monkeypatch, tmp_path):
    calls = []

    async def sample(ws, quick, profile):
        calls.append((quick, profile))
        return []

    monkeypatch.setattr(
        perf, "benchmark_registry", lambda profile: {"NP-99": ("Example", sample)}
    )
    monkeypatch.setenv("TEAMUP_RESULTS_DIR", str(tmp_path))

    assert perf.main(["--suite", "weekly", "--quick"]) == 0

    summary = json.loads((tmp_path / "weekly" / "perf.json").read_text())
    assert summary["suite"] == "weekly"
    assert [(row["id"], row["status"]) for row in summary["scenarios"]] == [
        ("NP-99", "pass"),
    ]
    assert calls == [(True, "weekly")]


def test_delivery_counts_are_specific_to_each_recipient():
    from tests.scenarios.perf.benchmarks import delivery_counts

    missing, duplicate, unexpected = delivery_counts(
        [{10, 11}, {12}],
        [[(12, 1.0), (12, 2.0)], [(10, 3.0), (12, 4.0)]],
    )
    assert (missing, duplicate, unexpected) == (1, 1, 1)


def test_interrupted_benchmark_keeps_completed_and_unfinished_verdicts(
    monkeypatch, tmp_path
):
    import pytest

    async def passed(ws, quick, profile):
        return []

    async def interrupted(ws, quick, profile):
        raise KeyboardInterrupt

    def registry(profile):
        return {"NP-01": ("Completed", passed), "NP-02": ("Interrupted", interrupted)}

    monkeypatch.setattr(perf, "benchmark_registry", registry)
    out = tmp_path / "summary.json"

    with pytest.raises(KeyboardInterrupt):
        perf.main(["--out", str(out)])

    assert [
        (row["id"], row["status"]) for row in json.loads(out.read_text())["scenarios"]
    ] == [("NP-01", "pass"), ("NP-02", "error")]
