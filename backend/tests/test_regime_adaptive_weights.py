"""
Tests unitaires — regime_adaptive_weights.py

Couvre :
  1. Caps N / delta (plus de blocage par N, seulement si EV absent)
  2. Sens EV / PF → ajustement correct
  3. Mode observe → jamais appliqué
  4. Résumé centré sur les régimes actuels
  5. Aucun dépassement de bornes
"""

import pytest
from unittest.mock import patch

from ..regime_adaptive_weights import (
    BASE_WEIGHTS,
    ADAPTIVE_WEIGHTS_MODE,
    _MAX_DELTA_LIGHT,
    _MAX_DELTA_FULL,
    _compute_weight_adjustment,
    _build_explanation,
    compute_adaptive_weights,
    format_adaptive_weights_report,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_perf(engine: str, regime: str, n: int, ev: float, pf: float):
    """Construit un faux résultat compute_regime_performance."""
    return {
        "status": "OK",
        "engine_matrix": {
            engine: {
                regime: {
                    "n": n,
                    "ev": ev,
                    "winrate": 55.0,
                    "profit_factor": pf,
                    "insufficient": n < 30,
                }
            }
        },
        "regime_distribution": {},
        "meta": {"n_events": n},
    }


# ── Caps N / delta ────────────────────────────────────────────────────────────

class TestGuardrailsN:
    def test_n_below_30_not_blocked_when_ev_available(self):
        delta, _tdelta, blocked, reason = _compute_weight_adjustment(29, ev=5.0, profit_factor=2.0)
        assert blocked is False
        assert delta > 0  # EV positif → delta positif, cap ±5%
        assert abs(delta) <= _MAX_DELTA_LIGHT + 1e-9

    def test_n_below_30_theoretical_delta_equals_delta(self):
        delta, tdelta, blocked, _ = _compute_weight_adjustment(5, ev=4.0, profit_factor=1.5)
        assert blocked is False
        assert tdelta == delta  # plus de distinction théorique/appliqué

    def test_n_exactly_1_allowed_with_ev(self):
        delta, _tdelta, blocked, _ = _compute_weight_adjustment(1, ev=4.0, profit_factor=1.5)
        assert blocked is False

    def test_n_99_max_delta_light(self):
        delta, _tdelta, blocked, _ = _compute_weight_adjustment(99, ev=4.0, profit_factor=2.0)
        assert blocked is False
        assert abs(delta) <= _MAX_DELTA_LIGHT + 1e-9

    def test_n_100_max_delta_full(self):
        delta, _tdelta, blocked, _ = _compute_weight_adjustment(100, ev=4.0, profit_factor=2.0)
        assert blocked is False
        assert abs(delta) <= _MAX_DELTA_FULL + 1e-9

    def test_delta_never_exceeds_max_full(self):
        delta, _tdelta, blocked, _ = _compute_weight_adjustment(500, ev=999.0, profit_factor=999.0)
        assert not blocked
        assert delta <= _MAX_DELTA_FULL

    def test_delta_never_below_neg_max_full(self):
        delta, _tdelta, blocked, _ = _compute_weight_adjustment(500, ev=-999.0, profit_factor=0.0)
        assert not blocked
        assert delta >= -_MAX_DELTA_FULL


# ── Sens EV ───────────────────────────────────────────────────────────────────

class TestEVSignal:
    def test_positive_ev_increases_delta(self):
        delta, _td, blocked, _ = _compute_weight_adjustment(100, ev=3.0, profit_factor=1.0)
        assert not blocked
        assert delta > 0

    def test_negative_ev_decreases_delta(self):
        delta, _td, blocked, _ = _compute_weight_adjustment(100, ev=-3.0, profit_factor=1.0)
        assert not blocked
        assert delta < 0

    def test_zero_ev_neutral_pf_no_change(self):
        delta, _td, blocked, _ = _compute_weight_adjustment(100, ev=0.0, profit_factor=1.0)
        assert not blocked
        assert delta == 0.0

    def test_no_ev_blocks(self):
        delta, _td, blocked, reason = _compute_weight_adjustment(100, ev=None, profit_factor=1.5)
        assert blocked is True
        assert delta == 0.0
        assert reason is not None


# ── Sens PF ───────────────────────────────────────────────────────────────────

class TestPFSignal:
    def test_pf_above_1_2_increases_delta(self):
        delta_good, _, _, _ = _compute_weight_adjustment(100, ev=0.0, profit_factor=1.5)
        delta_base, _, _, _ = _compute_weight_adjustment(100, ev=0.0, profit_factor=1.0)
        assert delta_good > delta_base

    def test_pf_below_1_decreases_delta(self):
        delta_bad,  _, _, _ = _compute_weight_adjustment(100, ev=0.0, profit_factor=0.5)
        delta_base, _, _, _ = _compute_weight_adjustment(100, ev=0.0, profit_factor=1.0)
        assert delta_bad < delta_base

    def test_pf_none_uses_ev_only(self):
        delta_pf, _td, blocked_pf, _ = _compute_weight_adjustment(100, ev=2.0, profit_factor=None)
        assert not blocked_pf
        # Doit quand même produire un delta positif via EV seul
        assert delta_pf > 0


# ── Explication lisible ───────────────────────────────────────────────────────

class TestBuildExplanation:
    def test_blocked_shows_blocked(self):
        expl = _build_explanation(
            "walls", "Panic", n=10, ev=None, winrate=None, profit_factor=None,
            delta=0.0, blocked=True, block_reason="N trop faible", proposed=1.0,
        )
        assert "BLOQUÉ" in expl
        assert "walls" in expl
        assert "Panic" in expl

    def test_increase_shows_arrow_up(self):
        expl = _build_explanation(
            "mopi", "Positive_Gamma", n=100, ev=2.5, winrate=60.0, profit_factor=1.5,
            delta=0.05, blocked=False, block_reason=None, proposed=1.05,
        )
        assert "↑" in expl

    def test_decrease_shows_arrow_down(self):
        expl = _build_explanation(
            "gex", "Negative_Gamma", n=100, ev=-2.0, winrate=45.0, profit_factor=0.7,
            delta=-0.04, blocked=False, block_reason=None, proposed=0.96,
        )
        assert "↓" in expl


# ── Mode observe ──────────────────────────────────────────────────────────────

class TestObserveMode:
    def test_mode_is_observe(self):
        assert ADAPTIVE_WEIGHTS_MODE == "observe"

    def test_base_weights_all_one(self):
        for engine, w in BASE_WEIGHTS.items():
            assert w == 1.0, f"{engine} base weight doit être 1.0"

    def test_base_weights_covers_all_engines(self):
        expected = {"squeeze", "walls", "gravity", "dealer", "mopi", "gex", "max_pain"}
        assert set(BASE_WEIGHTS.keys()) == expected

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_no_proposal_applied_in_observe(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 10_000_000, "iv_rank": 30, "dex": 100}
        mock_perf.return_value = _fake_perf("walls", "Positive_Gamma", n=150, ev=3.0, pf=1.8)

        report = compute_adaptive_weights(days=30)
        for p in report.proposals:
            assert p.applied is False, f"Mode observe → applied doit être False, pas {p}"


# ── compute_adaptive_weights ──────────────────────────────────────────────────

class TestComputeAdaptiveWeights:
    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_returns_report_structure(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 10_000_000, "iv_rank": 30, "dex": 100}
        mock_perf.return_value = _fake_perf("walls", "Positive_Gamma", n=50, ev=2.5, pf=1.4)

        report = compute_adaptive_weights(days=30)
        assert report.mode == "observe"
        assert isinstance(report.current_regime, list)
        assert len(report.current_regime) > 0
        assert isinstance(report.proposals, list)
        assert len(report.proposals) > 0
        assert isinstance(report.summary, dict)

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_n_below_30_not_blocked_when_ev_available(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 0, "iv_rank": None, "dex": None}
        mock_perf.return_value = _fake_perf("walls", "Neutral", n=10, ev=5.0, pf=3.0)

        report = compute_adaptive_weights(days=30)
        walls_neutral = [
            p for p in report.proposals
            if p.engine == "walls" and p.regime == "Neutral"
        ]
        assert walls_neutral, "Doit avoir une proposition walls × Neutral"
        assert walls_neutral[0].blocked is False
        assert walls_neutral[0].delta > 0  # EV positif → poids augmenté

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_summary_uses_current_regime_only(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 10_000_000, "iv_rank": 25, "dex": 100}
        mock_perf.return_value = _fake_perf("dealer", "Positive_Gamma", n=100, ev=2.0, pf=1.3)

        report = compute_adaptive_weights(days=30)
        # Structure de base
        assert isinstance(report.summary, dict)
        # Au moins un régime détecté (GEX > 5M garantit Positive_Gamma si mock actif,
        # ou un autre régime positif si DB réelle utilisée)
        assert len(report.current_regime) >= 1
        # Tous les moteurs de BASE_WEIGHTS doivent apparaître dans le résumé
        # (au moins un régime courant a une entrée, même bloquée → poids de base)
        for engine in BASE_WEIGHTS:
            assert engine in report.summary
        # Chaque valeur du résumé doit être un float positif
        for weight in report.summary.values():
            assert isinstance(weight, float)
            assert weight > 0

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_no_data_engine_all_blocked(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 0, "iv_rank": None, "dex": None}
        mock_perf.return_value = {
            "status": "OK",
            "engine_matrix": {},  # aucune donnée
            "regime_distribution": {},
            "meta": {"n_events": 0},
        }

        report = compute_adaptive_weights(days=30)
        # Tous les moteurs doivent être bloqués (aucune donnée)
        for p in report.proposals:
            assert p.blocked is True

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_proposed_weight_bounded(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": -20_000_000, "iv_rank": 80, "dex": 5000}
        mock_perf.return_value = _fake_perf("mopi", "Panic", n=200, ev=-10.0, pf=0.1)

        report = compute_adaptive_weights(days=30)
        for p in report.proposals:
            if not p.blocked:
                assert p.proposed_weight >= BASE_WEIGHTS[p.engine] - _MAX_DELTA_FULL - 1e-9
                assert p.proposed_weight <= BASE_WEIGHTS[p.engine] + _MAX_DELTA_FULL + 1e-9


# ── format_adaptive_weights_report ───────────────────────────────────────────

class TestFormatReport:
    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_format_keys(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 10_000_000, "iv_rank": 30, "dex": 100}
        mock_perf.return_value = _fake_perf("walls", "Positive_Gamma", n=50, ev=2.5, pf=1.4)

        report = compute_adaptive_weights(days=30)
        result = format_adaptive_weights_report(report)

        for key in ("mode", "current_regime", "summary", "weight_table", "stats",
                    "regime_distribution", "meta"):
            assert key in result, f"Clé manquante : {key}"

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_stats_totals(self, mock_perf, mock_state):
        mock_state.return_value = {"gex": 0, "iv_rank": None, "dex": None}
        mock_perf.return_value = {
            "status": "OK",
            "engine_matrix": {},
            "regime_distribution": {},
            "meta": {"n_events": 0},
        }

        report = compute_adaptive_weights(days=30)
        result = format_adaptive_weights_report(report)
        stats = result["stats"]

        total = stats["blocked"] + stats["increased"] + stats["decreased"] + stats["unchanged"]
        assert total == stats["total_proposals"]

    @patch("backend.regime_adaptive_weights._get_current_market_state")
    @patch("backend.regime_adaptive_weights.compute_regime_performance")
    def test_blocked_delta_is_zero_in_table(self, mock_perf, mock_state):
        """Un moteur bloqué doit avoir delta=0 dans le tableau formaté."""
        mock_state.return_value = {"gex": 0, "iv_rank": None, "dex": None}
        mock_perf.return_value = _fake_perf("gex", "Neutral", n=5, ev=3.0, pf=2.0)

        report = compute_adaptive_weights(days=30)
        result = format_adaptive_weights_report(report)

        neutral_table = result["weight_table"].get("Neutral", {})
        gex_entry = neutral_table.get("gex")
        if gex_entry and gex_entry["blocked"]:
            assert gex_entry["delta"] == 0.0
            assert gex_entry["proposed_weight"] == gex_entry["base_weight"]
