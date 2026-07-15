"""
test_coherence.py — Suite d'assertions de cohérence inter-moteurs (Phase 6).

12 invariants couvrant les phases P2/P3/P4/V5/F9.5/P0.5 :
  1.  conviction ≤ arbiter_confidence_pct // 10         (P2 cap souverain)
  2.  ProDecision.global_confidence == arbiter_confidence_pct (P3 alias)
  3.  ArbiteredDecision.global_confidence == confidence_pct   (P3 alias)
  4.  global_confidence ∈ [0, 100]                      (P3 bornes)
  5.  FL-0 → verdict == "OBSERVE"                       (V5 force_verdict)
  6.  NEU-0 → confidence_pct ≤ 20                       (V5 cap NEU)
  7.  min_dte ≤ 3 → signal_dte_degraded == True         (P4 TTL)
  8.  signal_dte_degraded → pre_expiration_warning non-None (P4 warning)
  9.  AGIR_* exige ≥2 signaux directionnels convergents (F9.5)
  10. data_quality INSUFFICIENT → system_status OFFLINE  (P0.5)
  11. contradictions + OBSERVE → system_status CONFLICT  (P0.5)
  12. arbiter_confidence_pct == 0 → conviction == 0      (P2 cap plancher)
"""
import sys
import os
import pytest

# ── Import depuis le répertoire parent (même pattern que les autres tests) ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.decision_arbiter import compute_decision, ArbiteredDecision

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_narrative(**overrides) -> dict:
    """Narrative minimale valide — aucun signal actif par défaut."""
    base = {
        "contradictions": [],
        "data_stale": False,
        "range_mode": False,
        "asymmetric_side": "NEUTRAL",
        "gex_regime": "NEUTRE",
        "dex_direction": "NEUTRAL",
        "dex_activity_context": "DEX dormant",
        "flip_activity_tag": "DORMANT",
    }
    base.update(overrides)
    return base


def _bullish_narrative() -> dict:
    """Narrative avec 2 signaux UP convergents (DEX + DB score 80)."""
    return _minimal_narrative(
        dex_direction="BULLISH_FLOWS",
        dex_use_in_signal=True,
    )


def _bearish_narrative() -> dict:
    """Narrative avec signal DEX baissier."""
    return _minimal_narrative(
        dex_direction="BEARISH_FLOWS",
        dex_use_in_signal=True,
    )


def _decision_with(**kwargs) -> ArbiteredDecision:
    """Raccourci : compute_decision avec une narrative minimale + overrides."""
    narrative = kwargs.pop("narrative", _minimal_narrative())
    return compute_decision(narrative, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 1 — Conviction cap P2 : conviction ≤ arbiter_confidence_pct // 10
# ─────────────────────────────────────────────────────────────────────────────
class TestConvictionCap:
    """Invariant 1 + 12 — le cap Arbiter est toujours respecté par ProDecision."""

    def test_low_arbiter_caps_conviction(self):
        """Arbiter 20% → conviction max 2/10."""
        res = _call_pro(arbiter_confidence_pct=20)
        assert res.conviction <= 2, (
            f"Arbiter 20% → conviction max 2, got {res.conviction}"
        )

    def test_zero_arbiter_gives_zero_conviction(self):
        """Invariant 12 : arbiter_confidence_pct == 0 → conviction == 0."""
        res = _call_pro(arbiter_confidence_pct=0)
        assert res.conviction == 0, (
            f"Arbiter 0% → conviction 0, got {res.conviction}"
        )

    def test_high_arbiter_does_not_inflate_conviction(self):
        """Arbiter 100% ne force pas la conviction au-delà de ce que le moteur calcule."""
        res = _call_pro(arbiter_confidence_pct=100)
        assert 0 <= res.conviction <= 10, f"Conviction hors bornes : {res.conviction}"

    def test_conviction_never_exceeds_cap(self):
        """Test paramétrique : pour diverses valeurs d'Arbiter, conviction ≤ cap."""
        for arb_pct in [0, 5, 10, 20, 35, 50, 65, 80, 100]:
            res = _call_pro(arbiter_confidence_pct=arb_pct)
            cap = arb_pct // 10
            assert res.conviction <= cap, (
                f"arbiter={arb_pct}% → cap={cap}, got conviction={res.conviction}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 2 — ProDecision.global_confidence == arbiter_confidence_pct (P3)
# ─────────────────────────────────────────────────────────────────────────────
class TestGlobalConfidenceAliasProDecision:
    """Invariant 2 : global_confidence est un alias exact de arbiter_confidence_pct."""

    def test_alias_matches_for_various_values(self):
        for pct in [0, 15, 42, 67, 100]:
            res = _call_pro(arbiter_confidence_pct=pct)
            assert res.global_confidence == pct, (
                f"global_confidence {res.global_confidence} ≠ arbiter_confidence_pct {pct}"
            )

    def test_alias_none_when_arbiter_not_provided(self):
        res = _call_pro(arbiter_confidence_pct=None)
        assert res.global_confidence is None, (
            f"global_confidence devrait être None sans Arbiter, got {res.global_confidence}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 3 — ArbiteredDecision.global_confidence == confidence_pct (P3)
# ─────────────────────────────────────────────────────────────────────────────
class TestGlobalConfidenceAliasArbiter:
    """Invariant 3 : global_confidence est un alias exact de confidence_pct."""

    def test_alias_matches_on_basic_decision(self):
        res = _decision_with()
        assert res.global_confidence == res.confidence_pct, (
            f"global_confidence {res.global_confidence} ≠ confidence_pct {res.confidence_pct}"
        )

    def test_alias_matches_signal_up(self):
        narr = _bullish_narrative()
        res = compute_decision(narr, directional_bias_score=80.0)
        assert res.global_confidence == res.confidence_pct


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 4 — global_confidence ∈ [0, 100]
# ─────────────────────────────────────────────────────────────────────────────
class TestGlobalConfidenceBounds:
    """Invariant 4 : global_confidence ne peut pas sortir de [0, 100]."""

    def test_bounds_neutral_regime(self):
        res = _decision_with()
        assert 0 <= res.global_confidence <= 100

    def test_bounds_with_boost_regime(self):
        res = compute_decision(
            _bullish_narrative(),
            vexcex_regime_id="EXP-UP-1",
            vexcex_phase="EXP",
            vexcex_urgency="ÉLEVÉE",
            directional_bias_score=85.0,
            pe_dominant_direction="BULL",
            pe_dominant_probability=70.0,
        )
        assert 0 <= res.global_confidence <= 100, f"Dépassement bornes : {res.global_confidence}"

    def test_bounds_with_heavy_degradation(self):
        res = compute_decision(
            _minimal_narrative(data_stale=True, contradictions=[{"detail": "GEX vs DEX"}]),
            vexcex_regime_id="NEU-0",
            vexcex_phase="NEU",
            signal_dte_context={"max_pain_dte": 1, "flip_top_dte": 1},
        )
        assert 0 <= res.global_confidence <= 100


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 5 — FL-0 force verdict OBSERVE (V5)
# ─────────────────────────────────────────────────────────────────────────────
class TestFL0ForceObserve:
    """Invariant 5 : FL-0 impose OBSERVE peu importe les autres signaux."""

    def test_fl0_overrides_bullish_signals(self):
        res = compute_decision(
            _bullish_narrative(),
            vexcex_regime_id="FL-0",
            vexcex_phase="FL",
            vexcex_urgency="CRITIQUE",
            directional_bias_score=90.0,
            pe_dominant_direction="BULL",
            pe_dominant_probability=75.0,
        )
        assert res.verdict == "OBSERVE", (
            f"FL-0 doit forcer OBSERVE, got {res.verdict}"
        )

    def test_fl0_overrides_bearish_signals(self):
        res = compute_decision(
            _bearish_narrative(),
            vexcex_regime_id="FL-0",
            vexcex_phase="FL",
            vexcex_urgency="ÉLEVÉE",
            directional_bias_score=-80.0,
        )
        assert res.verdict == "OBSERVE", (
            f"FL-0 doit forcer OBSERVE même avec signal baissier, got {res.verdict}"
        )

    def test_fl0_confidence_capped_at_30(self):
        res = compute_decision(
            _bullish_narrative(),
            vexcex_regime_id="FL-0",
            vexcex_phase="FL",
            vexcex_urgency="CRITIQUE",
            directional_bias_score=90.0,
        )
        assert res.confidence_pct <= 30, (
            f"FL-0 cap 30%, got {res.confidence_pct}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 6 — NEU-0 → confidence_pct ≤ 20 (V5 cap NEU)
# ─────────────────────────────────────────────────────────────────────────────
class TestNEU0ConfidenceCap:
    """Invariant 6 : NEU-0 plafonne la confiance à 20% même avec des signaux forts."""

    def test_neu0_cap_with_strong_signals(self):
        res = compute_decision(
            _bullish_narrative(),
            vexcex_regime_id="NEU-0",
            vexcex_phase="NEU",
            vexcex_urgency="MODÉRÉE",
            directional_bias_score=85.0,
            pe_dominant_direction="BULL",
            pe_dominant_probability=72.0,
        )
        assert res.confidence_pct <= 20, (
            f"NEU-0 cap 20%, got {res.confidence_pct}"
        )

    def test_neu1_cap_with_strong_signals(self):
        res = compute_decision(
            _bullish_narrative(),
            vexcex_regime_id="NEU-1",
            vexcex_phase="NEU",
            vexcex_urgency="MODÉRÉE",
            directional_bias_score=85.0,
        )
        assert res.confidence_pct <= 25, (
            f"NEU-1 cap 25%, got {res.confidence_pct}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 7 — min_dte ≤ 3 → signal_dte_degraded == True (P4 TTL)
# ─────────────────────────────────────────────────────────────────────────────
class TestTTLDegradation:
    """Invariant 7 : signal_dte_degraded activé dès que le DTE le plus court ≤ 3."""

    @pytest.mark.parametrize("min_dte", [0, 1, 2, 3])
    def test_dte_le_3_sets_degraded(self, min_dte):
        res = compute_decision(
            _minimal_narrative(),
            signal_dte_context={"max_pain_dte": min_dte, "flip_top_dte": 99},
        )
        assert res.signal_dte_degraded is True, (
            f"min_dte={min_dte} doit activer signal_dte_degraded"
        )

    def test_dte_4_does_not_set_degraded(self):
        res = compute_decision(
            _minimal_narrative(),
            signal_dte_context={"max_pain_dte": 4, "flip_top_dte": 99},
        )
        assert res.signal_dte_degraded is False, (
            f"min_dte=4 ne doit pas activer signal_dte_degraded"
        )

    def test_no_dte_context_no_degradation(self):
        res = compute_decision(_minimal_narrative())
        assert res.signal_dte_degraded is False


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 8 — signal_dte_degraded → pre_expiration_warning non-None (P4)
# ─────────────────────────────────────────────────────────────────────────────
class TestPreExpirationWarningConsistency:
    """Invariant 8 : warning toujours présent quand signal dégradé par TTL."""

    def test_warning_present_when_degraded(self):
        res = compute_decision(
            _minimal_narrative(),
            signal_dte_context={"max_pain_dte": 1, "flip_top_dte": 99},
        )
        assert res.signal_dte_degraded is True
        assert res.pre_expiration_warning is not None, (
            "pre_expiration_warning doit être non-None quand signal_dte_degraded"
        )

    def test_warning_absent_when_not_degraded(self):
        res = compute_decision(
            _minimal_narrative(),
            signal_dte_context={"max_pain_dte": 10, "flip_top_dte": 15},
        )
        assert res.signal_dte_degraded is False
        assert res.pre_expiration_warning is None, (
            "pre_expiration_warning doit être None quand pas de dégradation TTL"
        )

    def test_warning_mentions_expiry_label(self):
        """Le warning doit contenir le label J-N pour être informatif."""
        res = compute_decision(
            _minimal_narrative(),
            signal_dte_context={"max_pain_dte": 2, "flip_top_dte": 99},
        )
        assert res.pre_expiration_warning is not None
        assert "J-2" in res.pre_expiration_warning or "J-" in res.pre_expiration_warning, (
            f"Warning doit contenir 'J-N' : '{res.pre_expiration_warning}'"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 9 — AGIR_* exige ≥2 sources directionnelles convergentes (F9.5)
# ─────────────────────────────────────────────────────────────────────────────
class TestAgirRequiresTwoSources:
    """Invariant 9 : action AGIR_LONG/AGIR_SHORT impossible avec 1 seul signal directionnel."""

    def test_single_source_gives_preparer_not_agir(self):
        """DEX seul (1 source) → action PRÉPARER, jamais AGIR_*."""
        narr = _minimal_narrative(
            dex_direction="BULLISH_FLOWS",
            dex_use_in_signal=True,
        )
        # Pas de DB score, pas de PE → 1 seul signal DEX
        res = compute_decision(narr, directional_bias_score=None)
        if res.verdict in ("SIGNAL_UP", "SIGNAL_DOWN"):
            assert res.action != "AGIR_LONG" and res.action != "AGIR_SHORT", (
                f"1 source → action doit être PRÉPARER, got {res.action}"
            )

    def test_two_sources_can_give_agir(self):
        """DEX + DB score fort (≥70) → peut donner AGIR_* si confiance ≥60%."""
        narr = _minimal_narrative(
            dex_direction="BULLISH_FLOWS",
            dex_use_in_signal=True,
        )
        res = compute_decision(
            narr,
            directional_bias_score=80.0,  # 2e source
            vexcex_regime_id="EXP-UP-1",
            vexcex_phase="EXP",
            vexcex_urgency="ÉLEVÉE",
        )
        # Si verdict UP et conviction ≥ 60 et ≥2 sources → AGIR possible
        if res.verdict == "SIGNAL_UP" and res.confidence_pct >= 60:
            assert res.action == "AGIR_LONG", (
                f"2 sources + conf≥60% → AGIR_LONG attendu, got {res.action}"
            )

    def test_agir_never_with_insufficient_data(self):
        """data_quality INSUFFICIENT → verdict NO_TRADE, action OBSERVER, jamais AGIR_*."""
        res = compute_decision(
            _minimal_narrative(),
            health_data={"live_outcomes": 0},
        )
        assert res.action not in ("AGIR_LONG", "AGIR_SHORT"), (
            f"Données insuffisantes → jamais AGIR_*, got {res.action}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 10 — data_quality INSUFFICIENT → system_status OFFLINE (P0.5)
# ─────────────────────────────────────────────────────────────────────────────
class TestSystemStatusOffline:
    """Invariant 10 : données insuffisantes → system_status OFFLINE."""

    def test_insufficient_data_gives_offline(self):
        res = compute_decision(
            _minimal_narrative(),
            health_data={"live_outcomes": 5},  # < 10 → INSUFFICIENT
        )
        assert res.data_quality == "INSUFFICIENT"
        assert res.system_status == "OFFLINE", (
            f"INSUFFICIENT → OFFLINE, got {res.system_status}"
        )

    def test_sufficient_data_not_offline(self):
        res = compute_decision(
            _minimal_narrative(),
            health_data={"live_outcomes": 50},
        )
        assert res.system_status != "OFFLINE"


# ─────────────────────────────────────────────────────────────────────────────
# Invariant 11 — contradictions + OBSERVE → system_status CONFLICT (P0.5)
# ─────────────────────────────────────────────────────────────────────────────
class TestSystemStatusConflict:
    """Invariant 11 : signaux contradictoires + verdict OBSERVE → system_status CONFLICT."""

    def test_contradictions_give_conflict_status(self):
        narr = _minimal_narrative(
            contradictions=[{"detail": "GEX stabilisant vs DEX baissier"}],
            dex_direction="BEARISH_FLOWS",
            dex_use_in_signal=True,
        )
        res = compute_decision(narr)
        # Avec contradictions, verdict doit être OBSERVE
        assert res.verdict == "OBSERVE", f"Contradictions → OBSERVE, got {res.verdict}"
        assert res.system_status == "CONFLICT", (
            f"Contradictions + OBSERVE → CONFLICT, got {res.system_status}"
        )

    def test_no_contradiction_no_conflict(self):
        res = compute_decision(_minimal_narrative())
        assert res.system_status != "CONFLICT", (
            f"Sans contradictions, system_status ne doit pas être CONFLICT"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers pour les tests ProDecision (nécessitent un snapshot + args complets)
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_snap() -> dict:
    """Snapshot minimal acceptable par compute_pro_decision.

    Note: flip_level doit être non-None car _read_regime construit un dict
    avec un f-string ZONE_DE_FLIP évalué eagerly (bug latent prod).
    On passe 90000.0 pour éviter NoneType.__format__.
    """
    return {
        "spot": 95000.0,
        "dashboard": {
            "btc_price": 95000.0,
            "gex_regime": "NEUTRE",
            "gex_total": 0.0,
            "flip_level": 90000.0,   # non-None : évite le f-string ZONE_DE_FLIP sur None
            "iv_rank": 40.0,
        },
        "dealer": {
            "direction": "NEUTRAL",
            "net_delta": 0.0,
            "structural": 0.0,
            "actionable": 0.0,
            "intensity": "FAIBLE",
        },
        "walls": {
            "major_call_wall": None,
            "major_put_wall": None,
        },
    }


def _minimal_narrative_pro() -> dict:
    """Narrative minimale pour compute_pro_decision."""
    return {
        "contradictions": [],
        "data_stale": False,
        "range_mode": False,
        "asymmetric_side": "NEUTRAL",
        "gex_regime": "NEUTRE",
        "dex_direction": "NEUTRAL",
        "dex_use_in_signal": False,
        "dex_activity_context": "DEX dormant",
        "flip_activity_tag": "DORMANT",
        "flip_use_in_signal": False,
        "gex_use_in_signal": False,
        "vol_structure": [],
        "probability_engine": {},
    }


def _minimal_pe() -> dict:
    """Probability Engine minimal."""
    return {}


def _minimal_squeeze() -> dict:
    return {"score": 0, "label": "NEUTRE"}


def _minimal_db() -> dict:
    """Directional Bias minimal."""
    return {"score": 0.0, "direction": "NEUTRAL"}


def _minimal_gravity() -> dict:
    return {"gravity_magnet": None}


def _minimal_walls() -> dict:
    return {"major_call_wall": None, "major_put_wall": None}


def _call_pro(arbiter_confidence_pct=None, **kwargs):
    """Appelle compute_pro_decision avec tous les positional args requis.

    Signature réelle : snap, narrative, vol_structure_data, pe, squeeze,
                       directional_bias, gravity, walls, arbiter_confidence_pct=...
    """
    from backend.pro_decision_engine import compute_pro_decision
    snap = kwargs.pop("snap", _minimal_snap())
    narr = kwargs.pop("narr", _minimal_narrative_pro())
    return compute_pro_decision(
        snap,                   # snap
        narr,                   # narrative
        [],                     # vol_structure_data
        _minimal_pe(),          # pe
        _minimal_squeeze(),     # squeeze
        _minimal_db(),          # directional_bias
        _minimal_gravity(),     # gravity
        _minimal_walls(),       # walls
        arbiter_confidence_pct=arbiter_confidence_pct,
        **kwargs,
    )
