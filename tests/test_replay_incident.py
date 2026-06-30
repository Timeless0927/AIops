"""Tests for the replay eval harness (ADR-0003 child 3).

Strategy B (pure module logic): drive ``replay_incident.py`` directly. Async tests
run via the ``conftest.py`` runner — no ``@pytest.mark.asyncio``. Covers the
scoring tolerance matrix, the taxonomy self-check, and one full synthetic-fixture
replay end-to-end through ``run_diagnosis_session`` + ``ScriptedProvider``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import replay_incident as rp  # noqa: E402


# --- tolerance matrix: score_candidate branches -----------------------------

def test_score_exact() -> None:
    v = rp.score_candidate("resource_pressure_memory", "resource_pressure_memory", confidence=0.9)
    assert v["score"] == pytest.approx(1.0)
    assert v["hit"] is True
    assert v["reason"] == "exact+conf"


def test_score_exact_below_confidence_no_bonus() -> None:
    v = rp.score_candidate("config_error", "config_error", confidence=0.3)
    assert v["score"] == pytest.approx(1.0)  # exact is capped already at 1.0
    assert v["hit"] is True


def test_score_sibling() -> None:
    # memory vs cpu share the resource_pressure bucket
    v = rp.score_candidate("resource_pressure_cpu", "resource_pressure_memory", confidence=0.7)
    assert v["score"] == pytest.approx(0.6)  # 0.5 sibling + 0.1 conf
    assert v["hit"] is True
    assert v["reason"] == "sibling+conf"


def test_score_sibling_below_threshold_no_bonus() -> None:
    v = rp.score_candidate("resource_pressure_cpu", "resource_pressure_memory", confidence=0.4)
    assert v["score"] == pytest.approx(0.5)
    assert v["hit"] is True
    assert v["reason"] == "sibling"


def test_score_unrelated() -> None:
    v = rp.score_candidate("certificate_expiry", "resource_pressure_memory", confidence=0.9)
    assert v["score"] == 0.0
    assert v["hit"] is False
    assert v["reason"] == "unrelated"


def test_score_fallback_no_credit() -> None:
    # undifferentiated guess against a specific truth must never earn partial credit
    v = rp.score_candidate("undifferentiated", "resource_pressure_memory", confidence=0.95)
    assert v["score"] == 0.0
    assert v["hit"] is False
    assert v["reason"] == "fallback_no_credit"


def test_score_undifferentiated_vs_undifferentiated_exact() -> None:
    # an unknown-truth incident scored as undifferentiated is an exact match, not a fallback
    v = rp.score_candidate("undifferentiated", "undifferentiated", confidence=0.9)
    assert v["reason"] == "exact+conf"
    assert v["hit"] is True


def test_score_bonus_capped_at_one() -> None:
    v = rp.score_candidate("config_error", "config_error", confidence=0.99)
    assert v["score"] == 1.0


def test_score_case_insensitive() -> None:
    v = rp.score_candidate("Resource_Pressure_Memory", "resource_pressure_memory", confidence=0.7)
    assert v["score"] == pytest.approx(1.0)


# --- taxonomy self-check -----------------------------------------------------

def test_validate_taxonomy_clean() -> None:
    assert rp.validate_taxonomy() == []


def test_validate_taxonomy_detects_dangling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "ROOT_CAUSE_CATEGORIES", rp.ROOT_CAUSE_CATEGORIES | {"orphan_category"})
    problems = rp.validate_taxonomy()
    assert any("orphan_category" in p for p in problems)


# --- fixture loading + replay end-to-end ------------------------------------

def test_list_fixtures_finds_samples() -> None:
    names = {p.name for p in rp.list_fixtures()}
    assert {"synthetic-memory-pressure", "synthetic-cert-expiry"}.issubset(names)


def test_load_fixture_shape() -> None:
    fx = rp.load_fixture(rp.FIXTURES_ROOT / "synthetic-memory-pressure")
    assert fx["incident"]["incident_id"] == "synthetic-memory-pressure-001"
    assert fx["truth"]["root_cause_category"] == "resource_pressure_memory"
    assert len(fx["evidence"]) == 2
    assert {row["tool"] for row in fx["evidence"]} == {"query_metrics", "query_logs"}


async def test_replay_one_synthetic_memory_hits() -> None:
    fx = rp.load_fixture(rp.FIXTURES_ROOT / "synthetic-memory-pressure")
    result = await rp.replay_one(fx)
    assert result["synthetic"] is True
    assert result["predicted_category"] == "resource_pressure_memory"
    assert result["truth_category"] == "resource_pressure_memory"
    assert result["hit"] is True
    assert result["score"] == pytest.approx(1.0)
    assert result["status"] == "diagnosed"


async def test_replay_one_synthetic_cert_hits() -> None:
    fx = rp.load_fixture(rp.FIXTURES_ROOT / "synthetic-cert-expiry")
    result = await rp.replay_one(fx)
    assert result["truth_category"] == "certificate_expiry"
    assert result["predicted_category"] == "certificate_expiry"
    assert result["hit"] is True


async def test_run_sweep_separates_synthetic_from_real() -> None:
    report = await rp.run_sweep()
    assert report["synthetic_count"] >= 2
    # no real fixtures landed yet — hit-rate should be 0.0 with the placeholder note, not a crash
    assert report["real_count"] == 0
    assert report["real_hit_rate"] == 0.0
    synthetic_hits = [r for r in report["results"] if r["synthetic"] and r["hit"]]
    assert len(synthetic_hits) >= 2


def test_cli_validate_taxonomy_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert rp.main(["--validate-taxonomy"]) == 0
    out = capsys.readouterr().out
    assert "Taxonomy OK" in out


def test_cli_report_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert rp.main([]) == 0
    out = capsys.readouterr().out
    assert "Replay eval report" in out
    assert "operational campaign pending" in out  # placeholder until real fixtures land
    assert "synthetic-memory-pressure" in out


def test_cli_json_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert rp.main(["--json"]) == 0
    out = capsys.readouterr().out
    assert "synthetic-memory-pressure" in out
    assert "real_hit_rate" in out