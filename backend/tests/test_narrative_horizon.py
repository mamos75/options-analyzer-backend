"""
Tests — Narrative Horizon (V1 Hypothèse).

Scénarios obligatoires :
  1. horizon 4h  → DEX dominant
  2. horizon 24h → GEX dominant
  3. horizon 72h + expiry proche → Max Pain dominant
  4. GEX neutre → ignoré (veto)
  5. DEX dormant → ignoré (veto)
  6. Gravity dormant → ignoré (veto)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.narrative_resolver import resolve_narrative_horizon, HorizonNarrative
from backend.dealer_pressure import DealerPressure, DEXLevels
from backend.mopi import MOPIScore
from backend.gex import GEXProfile, MaxPainProfile, MaxPainExpiry
from backend.gravity_map import GravityMap
from backend.options_walls import OptionsWallsProfile
from backend.squeeze_score import SqueezeScore

BTC_SPOT = 100_000.0


# ─── Stubs ────────────────────────────────────────────────────────────────────

def _mopi(score: float = 60.0) -> MOPIScore:
    label = "BULLISH" if score >= 55 else ("BEARISH" if score < 45 else "NEUTRE")
    return MOPIScore(
        score=score, label=label, emoji="📊",
        gex_component=50.0, iv_rank_component=50.0,
        pc_ratio_component=50.0, squeeze_component=50.0,
        iv_rank=50.0, pc_ratio=1.0, squeeze_prob=30.0,
    )


def _gex(
    regime: str = "AMPLIFICATEUR",
    flip: float = 95_000.0,
    total_gex: float = 1_000_000_000.0,
    max_pain_dte: int = 7,
    max_pain_strike: float = 98_000.0,
) -> GEXProfile:
    mp_expiry = MaxPainExpiry(
        strike=max_pain_strike,
        expiry="2026-06-06",
        dte=max_pain_dte,
        oi_total=50_000,
    )
    return GEXProfile(
        total_gex=total_gex,
        gex_by_strike={},
        call_gex_by_strike={},
        put_gex_by_strike={},
        flip_level=flip,
        max_pain=max_pain_strike,
        gamma_walls=[],
        btc_price=BTC_SPOT,
        regime=regime,
        max_pain_profile=MaxPainProfile(near=mp_expiry, institutional=mp_expiry),
    )


def _gex_neutral() -> GEXProfile:
    """GEX neutre : |total_gex| < $5M."""
    return _gex(regime="NEUTRE", total_gex=2_000_000.0)


def _dp(direction: str = "BULLISH_FLOWS", net_delta: float = -2000.0) -> DealerPressure:
    nd = -abs(net_delta) if direction == "BULLISH_FLOWS" else (
        abs(net_delta) if direction == "BEARISH_FLOWS" else 0.0
    )
    color = "green" if direction == "BULLISH_FLOWS" else ("red" if direction == "BEARISH_FLOWS" else "yellow")
    risque = "SOUTIEN" if direction == "BULLISH_FLOWS" else ("RESISTANCE" if direction == "BEARISH_FLOWS" else "NEUTRE")
    return DealerPressure(
        net_delta=nd, net_delta_usd=nd * BTC_SPOT,
        delta_by_strike={}, direction=direction,
        intensity="MODERATE", pressure_pct=nd / 20_000 * 100,
        gauge_color=color, flux_conditionnel="...",
        direction_risque_trader=risque,
        exposition_nette_btc=abs(nd),
    )


def _gmap(has_magnetic_above: bool = False, has_magnetic_below: bool = False) -> GravityMap:
    from backend.gravity_map import GravityZone
    zones = []
    if has_magnetic_above:
        c = BTC_SPOT * 1.03
        zones.append(GravityZone(
            price_low=c * 0.99, price_high=c * 1.01, center=c,
            zone_type="MAGNETIC", strength=70.0,
            label="MAGNETIC", color="#fff", oi_usd=1e8, gex=5e6,
        ))
    if has_magnetic_below:
        c = BTC_SPOT * 0.97
        zones.append(GravityZone(
            price_low=c * 0.99, price_high=c * 1.01, center=c,
            zone_type="MAGNETIC", strength=70.0,
            label="MAGNETIC", color="#fff", oi_usd=1e8, gex=5e6,
        ))
    return GravityMap(
        btc_price=BTC_SPOT, zones=zones,
        strongest_magnet=BTC_SPOT * 1.02 if has_magnetic_above else BTC_SPOT,
        next_explosive=BTC_SPOT * 0.90,
        gravity_score=60.0,
        narrative="Gravité modérée.",
        timestamp=0.0,
    )


def _walls(active: bool = True) -> OptionsWallsProfile:
    from backend.options_walls import OptionsWall
    from backend.options_activity_engine import TAG_ACTIVE, TAG_DORMANT
    tag = TAG_ACTIVE if active else TAG_DORMANT
    wall = OptionsWall(
        strike=BTC_SPOT * 1.05, total_oi=10_000,
        call_oi=8_000, put_oi=2_000,
        notional_usd=1_000_000_000.0,
        wall_type="CALL_WALL", side="RESISTANCE",
        tag=tag,
    )
    return OptionsWallsProfile(
        walls=[wall],
        major_call_wall=BTC_SPOT * 1.05,
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


def _dex_levels(profile: str = "ACTIVE") -> DEXLevels:
    return DEXLevels(
        structural=-5000.0, active=-500.0, actionable=-50.0,
        structural_usd=-5_000_000_000.0, active_usd=-500_000_000.0, actionable_usd=-50_000_000.0,
        low_oi_anomaly_count=0, low_oi_anomaly_strikes=[],
        dex_profile=profile,
        dex_active_pct=40.0 if profile in ("ACTIVE", "ACTIONABLE") else 3.0,
        dex_actionable_pct=20.0 if profile == "ACTIONABLE" else 2.0,
    )


def _gravity_audit(all_dormant: bool = False):
    from backend.gravity_activity_audit import GravityActivityAudit, GravityZoneAudit
    from backend.gravity_activity_audit import GravityZoneCategoryBreakdown
    tag = "DORMANT" if all_dormant else "ACTIVE"
    use = not all_dormant

    def _breakdown():
        return GravityZoneCategoryBreakdown(oi_usd=0.0, oi_pct=0.0, count=0)

    zone = GravityZoneAudit(
        strike=BTC_SPOT * 1.02,
        zone_type="MAGNETIC",
        strength=70.0,
        oi_usd_total=100_000_000.0,
        contribution_pct=30.0,
        structural_score=100_000_000.0,
        active_score=40_000_000.0 if use else 0.0,
        actionable_score=28_000_000.0 if use else 0.0,
        dormant=_breakdown(),
        structural=_breakdown(),
        active=_breakdown(),
        actionable=_breakdown(),
        activity_tag=tag,
        activity_label="⚡ Gravity active" if use else "💀 Gravity dormante",
        activity_verdict="Active" if use else "Dormant",
        use_in_signal=use,
        signal_quality_score=7 if use else 1,
        signal_quality_label="Signal valide" if use else "Dormant",
        signal_quality_color="green" if use else "red",
    )
    return GravityActivityAudit(
        btc_price=BTC_SPOT,
        timestamp=0.0,
        total_gravity_oi_usd=100_000_000.0,
        global_dormant_pct=0.0 if use else 100.0,
        global_structural_pct=40.0,
        global_active_pct=60.0 if use else 0.0,
        global_actionable_pct=30.0 if use else 0.0,
        global_structural_score=100_000_000.0,
        global_active_score=60_000_000.0 if use else 0.0,
        global_actionable_score=30_000_000.0 if use else 0.0,
        global_active_engine_pct=60.0 if use else 0.0,
        global_actionable_engine_pct=50.0 if use else 0.0,
        overall_tag=tag,
        overall_label="⚡ Gravity active" if use else "💀 Gravity dormante",
        overall_verdict="Active" if use else "Dormant",
        signal_quality_score=7 if use else 1,
        signal_quality_label="Signal valide" if use else "Dormant",
        signal_quality_color="green" if use else "red",
        use_in_signal=use,
        zones=[zone],
    )


# ─── Test 1 : horizon 4h → DEX dominant ──────────────────────────────────────

def test_4h_dex_dominant():
    """4h : DEX (poids 1.0) doit être la force dominante, GEX poids 0.5."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert isinstance(result, HorizonNarrative)
    assert result.horizon == "4h"
    assert result.force_dominante == "DEX"
    assert result.hypothesis_version == "V1"
    assert result.hypothesis_disclaimer != ""
    # DEX actif bullish → dans forces_haussieres
    dex_forces = [f for f in result.forces_haussieres + result.forces_baissieres + result.forces_neutres
                  if f["name"] == "DEX"]
    assert len(dex_forces) == 1
    assert dex_forces[0]["weight"] == 1.0


# ─── Test 2 : horizon 24h → GEX dominant ─────────────────────────────────────

def test_24h_gex_dominant():
    """24h : GEX (poids 1.0) doit être la force dominante."""
    result = resolve_narrative_horizon(
        mopi=_mopi(65.0),
        gex=_gex(total_gex=500_000_000.0),  # GEX non-neutre
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert result.horizon == "24h"
    assert result.force_dominante == "GEX"
    gex_forces = [f for f in result.forces_haussieres + result.forces_baissieres + result.forces_neutres
                  if f["name"] == "GEX"]
    assert gex_forces[0]["weight"] == 1.0


# ─── Test 3 : horizon 72h + expiry proche → Max Pain dominant ────────────────

def test_72h_expiry_proche_max_pain_dominant():
    """72h avec DTE ≤ 3 : Max Pain doit être la force dominante."""
    result = resolve_narrative_horizon(
        mopi=_mopi(55.0),
        gex=_gex(max_pain_dte=2, max_pain_strike=98_000.0, total_gex=500_000_000.0),
        dp=_dp("NEUTRAL"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="72h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert result.horizon == "72h"
    assert result.force_dominante == "MAX_PAIN"
    mp_forces = [f for f in result.forces_haussieres + result.forces_baissieres + result.forces_neutres
                 if f["name"] == "MAX_PAIN"]
    assert len(mp_forces) == 1
    assert mp_forces[0]["weight"] == 1.0
    # Scénario doit mentionner l'expiry
    assert "Max Pain" in result.scenario or "expiry" in result.scenario.lower() or "J-2" in result.scenario


# ─── Test 4 : horizon 72h + DTE éloigné → Walls / Gravity dominants ──────────

def test_72h_no_expiry_walls_gravity_dominant():
    """72h avec DTE > 3 : Max Pain devient contexte, Walls ou Gravity dominent."""
    result = resolve_narrative_horizon(
        mopi=_mopi(55.0),
        gex=_gex(max_pain_dte=14, total_gex=500_000_000.0),
        dp=_dp("NEUTRAL"),
        gmap=_gmap(has_magnetic_above=True),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="72h",
        dex_levels=_dex_levels("ACTIVE"),
        gravity_audit=_gravity_audit(all_dormant=False),
    )
    assert result.horizon == "72h"
    assert result.force_dominante in ("WALLS", "GRAVITY")
    # Max Pain dans neutres ou baissieres avec poids réduit ≤ 0.1
    mp_forces = [f for f in result.forces_haussieres + result.forces_baissieres + result.forces_neutres
                 if f["name"] == "MAX_PAIN"]
    if mp_forces:
        assert mp_forces[0]["weight"] <= 0.1


# ─── Test 5 : GEX neutre → ignoré ────────────────────────────────────────────

def test_gex_neutre_veto():
    """GEX neutre (|total_gex| < $5M) → dans vetoed_forces, poids 0."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex_neutral(),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    gex_vetoed = [v for v in result.vetoed_forces if v["force"] == "GEX"]
    assert len(gex_vetoed) == 1, f"GEX aurait dû être veto, vetoed={result.vetoed_forces}"
    # GEX absent des forces actives
    all_forces = result.forces_haussieres + result.forces_baissieres + result.forces_neutres
    gex_active = [f for f in all_forces if f["name"] == "GEX"]
    assert len(gex_active) == 0


# ─── Test 6 : GEX régime NEUTRE → ignoré ─────────────────────────────────────

def test_gex_regime_neutre_veto():
    """GEX régime NEUTRE → veto même si total_gex > $5M."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(regime="NEUTRE", total_gex=100_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    gex_vetoed = [v for v in result.vetoed_forces if v["force"] == "GEX"]
    assert len(gex_vetoed) == 1


# ─── Test 7 : DEX dormant → ignoré ───────────────────────────────────────────

def test_dex_dormant_veto():
    """DEX DORMANT → use_in_signal=False → dans vetoed_forces, poids 0."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BEARISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("DORMANT"),
    )
    dex_vetoed = [v for v in result.vetoed_forces if v["force"] == "DEX"]
    assert len(dex_vetoed) == 1, f"DEX dormant aurait dû être veto, vetoed={result.vetoed_forces}"
    # DEX absent des forces actives
    all_forces = result.forces_haussieres + result.forces_baissieres + result.forces_neutres
    dex_active = [f for f in all_forces if f["name"] == "DEX"]
    assert len(dex_active) == 0
    # Avec DEX veto sur 4h, GEX (poids 0.5) prend la relève
    assert result.force_dominante != "AUCUNE"


# ─── Test 8 : DEX structurel → ignoré ────────────────────────────────────────

def test_dex_structural_veto():
    """DEX STRUCTURAL → use_in_signal=False → veto."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BEARISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("STRUCTURAL"),
    )
    dex_vetoed = [v for v in result.vetoed_forces if v["force"] == "DEX"]
    assert len(dex_vetoed) == 1


# ─── Test 9 : Gravity dormante → ignorée ─────────────────────────────────────

def test_gravity_dormant_veto():
    """Toutes zones Gravity DORMANT → dans vetoed_forces, poids 0."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
        gravity_audit=_gravity_audit(all_dormant=True),
    )
    gravity_vetoed = [v for v in result.vetoed_forces if v["force"] == "GRAVITY"]
    assert len(gravity_vetoed) == 1
    all_forces = result.forces_haussieres + result.forces_baissieres + result.forces_neutres
    gravity_active = [f for f in all_forces if f["name"] == "GRAVITY"]
    assert len(gravity_active) == 0


# ─── Test 10 : Output structure complète ─────────────────────────────────────

def test_output_structure():
    """HorizonNarrative contient tous les champs requis."""
    result = resolve_narrative_horizon(
        mopi=_mopi(),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp(),
        gmap=_gmap(),
        walls=_walls(),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels(),
    )
    assert isinstance(result.horizon, str)
    assert isinstance(result.force_dominante, str)
    assert isinstance(result.scenario, str) and len(result.scenario) > 0
    assert isinstance(result.niveau_haut, float) and result.niveau_haut > 0
    assert isinstance(result.niveau_bas, float) and result.niveau_bas > 0
    assert isinstance(result.forces_haussieres, list)
    assert isinstance(result.forces_baissieres, list)
    assert isinstance(result.forces_neutres, list)
    assert isinstance(result.confidence, int)
    assert 10 <= result.confidence <= 100
    assert result.hypothesis_version == "V1"
    assert "backtest" in result.hypothesis_disclaimer.lower()
    assert isinstance(result.vetoed_forces, list)


# ─── Test 11 : Erreur sur horizon invalide ────────────────────────────────────

def test_invalid_horizon_raises():
    import pytest
    with pytest.raises(ValueError, match="horizon"):
        resolve_narrative_horizon(
            mopi=_mopi(), gex=_gex(), dp=_dp(), gmap=_gmap(),
            walls=_walls(), sq=_sq(), spot=BTC_SPOT, horizon="1h",
        )


# ─── Test 12 : confidence borné 10-100 ───────────────────────────────────────

def test_confidence_bounds():
    """Confidence toujours dans [10, 100], même cas extrêmes."""
    # Cas : tout veto
    result = resolve_narrative_horizon(
        mopi=_mopi(50.0),
        gex=_gex_neutral(),
        dp=_dp("NEUTRAL"),
        gmap=_gmap(),
        walls=_walls(active=False),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("DORMANT"),
        gravity_audit=_gravity_audit(all_dormant=True),
    )
    assert 10 <= result.confidence <= 100


# ─── Test 13 : force_dominante jamais dans forces_neutres ────────────────────

def test_force_dominante_never_in_forces_neutres():
    """force_dominante ne peut jamais être une force dans forces_neutres."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    neutres_names = {f["name"] for f in result.forces_neutres}
    if result.force_dominante != "AUCUNE":
        assert result.force_dominante not in neutres_names, (
            f"force_dominante '{result.force_dominante}' est dans forces_neutres {neutres_names}"
        )


# ─── Test 14 : force_dominante jamais dans vetoed_forces ─────────────────────

def test_force_dominante_never_vetoed():
    """force_dominante ne peut jamais être une force vetoed."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("DORMANT"),
    )
    vetoed_names = {v["force"] for v in result.vetoed_forces}
    if result.force_dominante != "AUCUNE":
        assert result.force_dominante not in vetoed_names, (
            f"force_dominante '{result.force_dominante}' est dans vetoed_forces {vetoed_names}"
        )


# ─── Test 15 : toutes forces neutres → AUCUNE ────────────────────────────────

def test_all_directional_forces_neutral_returns_aucune():
    """GEX STABILISANT + DEX NEUTRAL + GRAVITY symétrique + WALLS symétriques
    → toutes forces non-directionnelles → force_dominante = AUCUNE."""
    result = resolve_narrative_horizon(
        mopi=_mopi(50.0),
        gex=_gex(regime="STABILISANT", total_gex=500_000_000.0, max_pain_strike=BTC_SPOT),
        dp=_dp("NEUTRAL"),
        gmap=_gmap(has_magnetic_above=True, has_magnetic_below=True),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
        gravity_audit=_gravity_audit(all_dormant=False),
    )
    assert result.force_dominante == "AUCUNE", (
        f"Toutes forces neutres → attendu AUCUNE, got '{result.force_dominante}'"
    )


# ─── Test 16 : GEX neutre ne peut pas gagner uniquement par le poids ─────────

def test_gex_stabilisant_cannot_win_by_weight():
    """GEX STABILISANT (poids 1.0 en 24h, direction NEUTRE) ne peut pas être
    force_dominante. DEX BULLISH (poids 0.5) doit prendre le relais."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(regime="STABILISANT", total_gex=500_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert result.force_dominante != "GEX", (
        f"GEX STABILISANT (NEUTRE) ne doit pas être force_dominante, got '{result.force_dominante}'"
    )
    gex_in_neutres = any(f["name"] == "GEX" for f in result.forces_neutres)
    assert gex_in_neutres, "GEX STABILISANT doit être dans forces_neutres"
    assert result.force_dominante == "DEX"


# ─── Test 17 : Walls peuvent gagner si seule force directionnelle ─────────────

def test_walls_wins_when_only_directional_force():
    """GEX neutre (veto) + DEX NEUTRAL + GRAVITY vetoed → WALLS haussier gagne."""
    from backend.options_walls import OptionsWallsProfile, OptionsWall
    from backend.options_activity_engine import TAG_ACTIVE

    wall = OptionsWall(
        strike=BTC_SPOT * 1.20, total_oi=10_000, call_oi=8_000, put_oi=2_000,
        notional_usd=1_000_000_000.0, wall_type="CALL_WALL", side="RESISTANCE",
        tag=TAG_ACTIVE,
    )
    walls_haussier = OptionsWallsProfile(
        walls=[wall],
        major_call_wall=BTC_SPOT * 1.20,   # +20% — loin
        major_put_wall=BTC_SPOT * 0.95,     # -5% — proche → HAUSSIER
        oi_by_strike={}, btc_price=BTC_SPOT,
    )
    result = resolve_narrative_horizon(
        mopi=_mopi(50.0),
        gex=_gex(regime="NEUTRE", total_gex=100_000_000.0),  # vetoed
        dp=_dp("NEUTRAL"),
        gmap=_gmap(),
        walls=walls_haussier,
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
    )
    assert result.force_dominante == "WALLS", (
        f"WALLS haussier devrait être force_dominante, got '{result.force_dominante}'"
    )


# ─── Test 18 : Gravity peut gagner si seule force directionnelle ──────────────

def test_gravity_wins_when_only_directional_force():
    """GEX neutre (veto) + DEX NEUTRAL + WALLS symétriques → GRAVITY haussière gagne."""
    result = resolve_narrative_horizon(
        mopi=_mopi(50.0),
        gex=_gex(regime="NEUTRE", total_gex=2_000_000.0),   # vetoed
        dp=_dp("NEUTRAL"),
        gmap=_gmap(has_magnetic_above=True, has_magnetic_below=False),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="24h",
        dex_levels=_dex_levels("ACTIVE"),
        gravity_audit=_gravity_audit(all_dormant=False),
    )
    assert result.force_dominante == "GRAVITY", (
        f"GRAVITY haussière devrait être force_dominante, got '{result.force_dominante}'"
    )


# ─── Test 19 : DEX veto → ne peut pas être force_dominante ───────────────────

def test_dex_veto_cannot_be_force_dominante():
    """DEX DORMANT (vetoed) ne peut pas être force_dominante même à horizon 4h
    où son poids nominal est 1.0."""
    result = resolve_narrative_horizon(
        mopi=_mopi(60.0),
        gex=_gex(total_gex=500_000_000.0),
        dp=_dp("BULLISH_FLOWS"),
        gmap=_gmap(),
        walls=_walls(active=True),
        sq=_sq(),
        spot=BTC_SPOT,
        horizon="4h",
        dex_levels=_dex_levels("DORMANT"),
    )
    assert result.force_dominante != "DEX", (
        f"DEX DORMANT (vetoed) ne doit pas être force_dominante, got '{result.force_dominante}'"
    )
    vetoed_names = {v["force"] for v in result.vetoed_forces}
    assert "DEX" in vetoed_names
