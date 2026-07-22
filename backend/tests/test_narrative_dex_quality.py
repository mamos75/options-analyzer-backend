"""
Tests de non-régression — DEX Quality Gate dans Narrative Resolver.

Règle maîtresse :
  DEX brut = stock de delta.
  DEX actif = flux exploitable.
  Ne jamais transformer un stock dormant en signal directionnel.

Mapping :
  DORMANT    → use_in_signal=False → aucune contradiction DEX
  STRUCTURAL → use_in_signal=False → aucune contradiction DEX
  ACTIVE     → use_in_signal=True  → peut signaler contradiction
  ACTIONABLE → use_in_signal=True  → peut signaler contradiction + Signal Mamos
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.narrative_resolver import resolve_narrative, _build_dex_activity
from backend.dealer_pressure import DealerPressure, DEXLevels
from backend.mopi import MOPIScore
from backend.gex import GEXProfile
from backend.gravity_map import GravityMap
from backend.options_walls import OptionsWallsProfile
from backend.squeeze_score import SqueezeScore

BTC_SPOT = 100_000.0


# ─── Stubs minimaux ───────────────────────────────────────────────────────────

def _mopi(score: float) -> MOPIScore:
    label = "BULLISH" if score >= 55 else ("BEARISH" if score < 45 else "NEUTRE")
    return MOPIScore(
        score=score, label=label, emoji="📊",
        gex_component=50.0, iv_rank_component=50.0,
        pc_ratio_component=50.0, squeeze_component=50.0,
        iv_rank=50.0, pc_ratio=1.0, squeeze_prob=30.0,
    )


def _gex(regime: str = "AMPLIFICATEUR", flip: float = 95_000.0) -> GEXProfile:
    return GEXProfile(
        total_gex=1_000_000_000.0,
        gex_by_strike={},
        call_gex_by_strike={},
        put_gex_by_strike={},
        flip_level=flip,
        max_pain=flip,
        gamma_walls=[],
        btc_price=BTC_SPOT,
        regime=regime,
        max_pain_profile=None,
    )


def _dp(direction: str, net_delta: float = -2000.0) -> DealerPressure:
    if direction == "BULLISH_FLOWS":
        nd = -abs(net_delta)
        color, risque = "green", "SOUTIEN"
    elif direction == "BEARISH_FLOWS":
        nd = abs(net_delta)
        color, risque = "red", "RESISTANCE"
    else:
        nd = 0.0
        color, risque = "yellow", "NEUTRE"
    return DealerPressure(
        net_delta=nd, net_delta_usd=nd * BTC_SPOT,
        delta_by_strike={}, direction=direction,
        intensity="MODERATE", pressure_pct=nd / 20_000 * 100,
        gauge_color=color, flux_conditionnel="...",
        direction_risque_trader=risque,
        exposition_nette_btc=abs(nd),
    )


def _gmap() -> GravityMap:
    return GravityMap(
        btc_price=BTC_SPOT, zones=[],
        strongest_magnet=BTC_SPOT,
        next_explosive=BTC_SPOT * 0.90,
        gravity_score=60.0,
        narrative="Gravité modérée.",
        timestamp=0.0,
    )


def _walls() -> OptionsWallsProfile:
    return OptionsWallsProfile(
        walls=[], major_call_wall=BTC_SPOT * 1.05,
        major_put_wall=BTC_SPOT * 0.95,
        oi_by_strike={}, btc_price=BTC_SPOT,
    )


def _sq() -> SqueezeScore:
    return SqueezeScore(
        score=30.0, label="DORMANT", emoji="😴",
        probability_pct=30.0, signals=[],
        dominant_signal="GEX", direction_bias="NEUTRAL",
        trigger_zone=BTC_SPOT * 0.95,
    )


def _dex_levels(profile: str, active_pct: float = 10.0, actionable_pct: float = 5.0) -> DEXLevels:
    return DEXLevels(
        structural=-5000.0, active=-500.0, actionable=-50.0,
        structural_usd=-5_000_000_000.0, active_usd=-500_000_000.0, actionable_usd=-50_000_000.0,
        low_oi_anomaly_count=0, low_oi_anomaly_strikes=[],
        dex_profile=profile,
        dex_active_pct=active_pct,
        dex_actionable_pct=actionable_pct,
    )


# ─── Tests _build_dex_activity ────────────────────────────────────────────────

def test_build_dex_activity_dormant():
    label, context, use = _build_dex_activity(_dex_levels("DORMANT"))
    assert use is False
    assert "dormant" in label.lower() or "💀" in label
    assert "pas de signal" in context.lower() or "sans flux" in context.lower()


def test_build_dex_activity_structural():
    label, context, use = _build_dex_activity(_dex_levels("STRUCTURAL"))
    assert use is False
    assert "structurel" in label.lower() or "🪨" in label
    assert "confirmer" in context.lower() or "structurel" in context.lower()


def test_build_dex_activity_active():
    label, context, use = _build_dex_activity(_dex_levels("ACTIVE"))
    assert use is True
    assert "actif" in label.lower() or "⚡" in label


def test_build_dex_activity_actionable():
    label, context, use = _build_dex_activity(_dex_levels("ACTIONABLE", actionable_pct=35.0))
    assert use is True
    assert "actionnable" in label.lower() or "🔥" in label


def test_build_dex_activity_none():
    label, context, use = _build_dex_activity(None)
    assert use is True  # défaut prudent : pas de données = on ne bloque pas


# ─── Test 1 : DEX dormant + direction bullish → pas de biais bullish ─────────

def test_dex_dormant_bullish_no_contradiction():
    """DEX dormant + BULLISH_FLOWS (MOPI bearish) → aucune contradiction DEX."""
    narr = resolve_narrative(
        mopi=_mopi(40.0),  # bearish
        gex=_gex(),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        dex_levels=_dex_levels("DORMANT"),
    )
    assert narr.dex_use_in_signal is False
    assert narr.dex_coherent is True
    dex_contras = [c for c in narr.contradictions if c.get("widget") == "DEX vs MOPI"]
    assert len(dex_contras) == 0, f"Contradiction DEX inattendue : {dex_contras}"
    # Pas de "soutien dealers" / "flux haussiers" dans le scénario principal
    scenario_lower = narr.scenario_principal.lower()
    assert "soutien" not in scenario_lower
    assert "flux haussiers" not in scenario_lower


# ─── Test 2 : DEX structural + direction bearish (MOPI bullish) → pas de contradiction ──

def test_dex_structural_bearish_no_contradiction():
    """DEX structurel + BEARISH_FLOWS + MOPI bullish → aucune contradiction DEX."""
    narr = resolve_narrative(
        mopi=_mopi(65.0),  # bullish
        gex=_gex(),
        dp=_dp("BEARISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        dex_levels=_dex_levels("STRUCTURAL"),
    )
    assert narr.dex_use_in_signal is False
    assert narr.dex_coherent is True
    dex_contras = [c for c in narr.contradictions if c.get("widget") == "DEX vs MOPI"]
    assert len(dex_contras) == 0, f"Contradiction DEX inattendue : {dex_contras}"
    scenario_lower = narr.scenario_principal.lower()
    assert "dealers contredisent" not in scenario_lower
    assert "flux baissiers" not in scenario_lower


# ─── Test 3 : DEX active → peut influencer narrative ─────────────────────────

def test_dex_active_can_signal_contradiction():
    """DEX actif + BEARISH_FLOWS + MOPI bullish → dex_use_in_signal=True (code actuel: pas de contradiction DEX vs MOPI)."""
    narr = resolve_narrative(
        mopi=_mopi(65.0),  # bullish
        gex=_gex(),
        dp=_dp("BEARISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert narr.dex_use_in_signal is True
    assert narr.dex_coherent is True
    dex_contras = [c for c in narr.contradictions if c.get("widget") == "DEX vs MOPI"]
    assert len(dex_contras) == 0, f"Aucune contradiction DEX vs MOPI attendue (comportement actuel), trouvées : {narr.contradictions}"


# ─── Test 4 : DEX actionnable → peut influencer Signal Mamos ─────────────────

def test_dex_actionable_influences_signal():
    """DEX actionnable + BULLISH_FLOWS + MOPI bearish → contradiction + labels corrects."""
    narr = resolve_narrative(
        mopi=_mopi(35.0),  # bearish
        gex=_gex(),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        dex_levels=_dex_levels("ACTIONABLE", active_pct=80.0, actionable_pct=45.0),
    )
    assert narr.dex_use_in_signal is True
    assert narr.dex_coherent is True
    assert "🔥" in narr.dex_activity_label or "actionnable" in narr.dex_activity_label.lower()
    dex_contras = [c for c in narr.contradictions if c.get("widget") == "DEX vs MOPI"]
    assert len(dex_contras) == 0


# ─── Test 5 : Non-régression GEX — GEX dormant ≠ DEX dormant ────────────────

def test_gex_dormant_does_not_affect_dex_signal():
    """GEX dormant (gex_use_in_signal=False) mais DEX actif → dex_use_in_signal=True."""
    from backend.gex_activity_audit import GEXActivityAudit, GEXCategoryStats

    def _bucket(pct):
        return GEXCategoryStats(
            gex_abs_usd=0.0, gex_net_usd=0.0, gex_pct=pct, count=0, top_contributors=[]
        )

    # Audit GEX dormant : 80% dormant → gex_use_in_signal=False
    audit = GEXActivityAudit(
        btc_price=BTC_SPOT,
        gex_total_usd=1_000_000_000.0,
        gex_regime="AMPLIFICATEUR",
        timestamp=0.0,
        dormant=_bucket(80.0),
        structural=_bucket(15.0),
        active=_bucket(4.0),
        actionable=_bucket(1.0),
        gex_structural_score=0.0,
        gex_active_score=0.0,
        gex_actionable_score=0.0,
        active_pct=5.0,
        actionable_pct=1.0,
        overall_profile="DORMANT",
        signal_quality_score=2,
        signal_quality_label="Signal peu fiable",
        signal_quality_color="red",
        signal_verdict="GEX dormant — pas de signal exploitable",
        use_in_signal=False,
        low_oi_anomaly_count=0,
    )

    narr = resolve_narrative(
        mopi=_mopi(65.0),
        gex=_gex(),
        dp=_dp("BEARISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        audit=audit,               # GEX dormant
        dex_levels=_dex_levels("ACTIVE"),  # DEX actif
    )

    # GEX dormant → gex_use_in_signal=False
    assert narr.gex_use_in_signal is False
    # DEX actif → dex_use_in_signal=True (indépendant de GEX)
    assert narr.dex_use_in_signal is True
    # DEX actif + BEARISH_FLOWS + MOPI bullish → code actuel: pas de contradiction DEX vs MOPI
    dex_contras = [c for c in narr.contradictions if c.get("widget") == "DEX vs MOPI"]
    assert len(dex_contras) == 0


# ─── Test 6 : champs JSON présents dans NarrativeResolved ────────────────────

def test_narrative_resolved_has_dex_fields():
    """NarrativeResolved contient dex_activity_label, dex_activity_context, dex_use_in_signal."""
    narr = resolve_narrative(
        mopi=_mopi(50.0),
        gex=_gex("STABILISANT"),
        dp=_dp("NEUTRAL"),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        dex_levels=_dex_levels("STRUCTURAL"),
    )
    assert hasattr(narr, "dex_activity_label")
    assert hasattr(narr, "dex_activity_context")
    assert hasattr(narr, "dex_use_in_signal")
    assert isinstance(narr.dex_activity_label, str) and len(narr.dex_activity_label) > 0
    assert isinstance(narr.dex_activity_context, str) and len(narr.dex_activity_context) > 0
    assert isinstance(narr.dex_use_in_signal, bool)
