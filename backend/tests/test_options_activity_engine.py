"""
Tests de non-régression — options_activity_engine.py

Couvre les cas spécifiés dans P1 :
  1. DEX structural ≠ DEX active (valeurs différentes)
  2. volume=0 → active faible (≈ 0)
  3. gros OI dormant → structural élevé mais actionable faible
  4. petit OI + gros volume → low_oi_anomaly
  5. wall proche + volume élevé + DTE court → ACTIONABLE
  6. wall lointain + volume faible → DORMANT
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.deribit_client import OptionData, MarketSnapshot
from backend.options_activity_engine import (
    compute_flow_ratio,
    compute_proximity_score,
    compute_dte_urgency,
    classify_activity_profile,
    compute_structural_active_actionable,
    ActivityScores,
    TAG_DORMANT, TAG_STRUCTURAL, TAG_ACTIVE, TAG_ACTIONABLE,
    MIN_OI_FLOW, FLOW_RATIO_CAP, LOW_OI_ANOMALY_OI,
    CONTRACT_SIZE,
)

BTC_SPOT = 95_000.0
EXPIRY_NEAR = "01JUN30"   # DTE > 0
EXPIRY_FAR  = "26DEC30"   # DTE >> 30


def _opt(strike, opt_type, oi, volume=0.0, delta=None, expiry=EXPIRY_NEAR):
    if delta is None:
        delta = 0.5 if opt_type == "call" else -0.5
    return OptionData(
        instrument=f"BTC-{expiry}-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry=expiry,
        option_type=opt_type,
        oi=oi,
        volume=volume,
        gamma=0.0001,
        delta=delta,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )


def _snap(options):
    return MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)


# ─── compute_flow_ratio ───────────────────────────────────────────────────────

def test_flow_ratio_below_min_oi():
    """OI < MIN_OI_FLOW → 0.0, pas d'anomalie."""
    opt = _opt(BTC_SPOT, "call", oi=MIN_OI_FLOW - 1, volume=100.0)
    ratio, anomaly = compute_flow_ratio(opt)
    assert ratio == 0.0
    assert anomaly is False


def test_flow_ratio_capped():
    """Volume > OI sur grand OI → cappé à 1.0."""
    opt = _opt(BTC_SPOT, "call", oi=1000, volume=5000.0)
    ratio, anomaly = compute_flow_ratio(opt)
    assert ratio == FLOW_RATIO_CAP == 1.0
    assert anomaly is False


def test_flow_ratio_normal():
    """Volume/OI = 0.4 → flow = 0.4."""
    opt = _opt(BTC_SPOT, "call", oi=500, volume=200.0)
    ratio, anomaly = compute_flow_ratio(opt)
    assert abs(ratio - 0.4) < 0.001
    assert anomaly is False


def test_flow_ratio_low_oi_anomaly():
    """OI < LOW_OI_ANOMALY_OI + volume > OI → anomalie."""
    opt = _opt(BTC_SPOT, "call", oi=LOW_OI_ANOMALY_OI - 1, volume=float(LOW_OI_ANOMALY_OI))
    ratio, anomaly = compute_flow_ratio(opt)
    assert ratio == FLOW_RATIO_CAP
    assert anomaly is True


# ─── compute_proximity_score ──────────────────────────────────────────────────

def test_proximity_at_spot():
    assert compute_proximity_score(BTC_SPOT, BTC_SPOT) == 1.0


def test_proximity_10pct():
    assert compute_proximity_score(BTC_SPOT * 1.10, BTC_SPOT) == 0.0


def test_proximity_5pct():
    val = compute_proximity_score(BTC_SPOT * 1.05, BTC_SPOT)
    assert abs(val - 0.5) < 0.001


def test_proximity_beyond_10pct():
    """Distance > 10% → 0.0 (pas négatif)."""
    assert compute_proximity_score(BTC_SPOT * 1.20, BTC_SPOT) == 0.0


# ─── compute_dte_urgency ──────────────────────────────────────────────────────

def test_dte_expired():
    assert compute_dte_urgency(0) == 0.0


def test_dte_day1():
    assert compute_dte_urgency(1) == 1.0


def test_dte_day3():
    assert compute_dte_urgency(3) == 0.8


def test_dte_day7():
    assert compute_dte_urgency(7) == 0.5


def test_dte_day30():
    assert compute_dte_urgency(30) == 0.15


def test_dte_far():
    assert compute_dte_urgency(90) == 0.05


# ─── classify_activity_profile ───────────────────────────────────────────────

def test_classify_dormant():
    assert classify_activity_profile(active_pct=2.0, actionable_pct=0.0) == TAG_DORMANT


def test_classify_structural():
    assert classify_activity_profile(active_pct=20.0, actionable_pct=5.0) == TAG_STRUCTURAL


def test_classify_active():
    assert classify_activity_profile(active_pct=30.0, actionable_pct=20.0) == TAG_ACTIVE


def test_classify_actionable():
    assert classify_activity_profile(active_pct=50.0, actionable_pct=40.0) == TAG_ACTIONABLE


# ─── Cas 1 : DEX structural ≠ DEX active ─────────────────────────────────────

def test_dex_structural_differs_from_active():
    """Structural inclut tout l'OI, active seulement l'OI avec flux — valeurs différentes."""
    opts = [
        _opt(BTC_SPOT, "call", oi=1000, volume=200.0, delta=0.6),   # flow = 0.2
        _opt(BTC_SPOT, "put",  oi=800,  volume=0.0,   delta=-0.4),  # flow = 0
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=True)
    assert scores.structural != scores.active, (
        f"structural={scores.structural} devrait ≠ active={scores.active}"
    )


# ─── Cas 2 : volume=0 → active ≈ 0 ──────────────────────────────────────────

def test_zero_volume_gives_zero_active():
    """Sans volume, flow_ratio=0 → active=0 et actionable=0."""
    opts = [
        _opt(BTC_SPOT, "call", oi=2000, volume=0.0, delta=0.6),
        _opt(BTC_SPOT, "put",  oi=1500, volume=0.0, delta=-0.4),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT)
    assert scores.active == 0.0, f"active={scores.active} devrait être 0"
    assert scores.actionable == 0.0


# ─── Cas 3 : gros OI dormant → structural élevé, actionable faible ───────────

def test_large_oi_no_volume_dormant():
    """Gros OI sans volume → structural grand, active=0, profil DORMANT."""
    opts = [
        # 5000 OI au spot, aucun volume
        _opt(BTC_SPOT, "call", oi=5000, volume=0.0, delta=0.5),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=False)
    assert abs(scores.structural) > 1000, "structural doit être grand"
    assert scores.active == 0.0
    assert scores.actionable == 0.0
    assert scores.profile == TAG_DORMANT


# ─── Cas 4 : petit OI + gros volume → low_oi_anomaly ────────────────────────

def test_small_oi_large_volume_anomaly():
    """OI < LOW_OI_ANOMALY_OI + volume > OI → low_oi_anomaly_count = 1."""
    opts = [
        _opt(BTC_SPOT, "call", oi=LOW_OI_ANOMALY_OI - 1, volume=float(LOW_OI_ANOMALY_OI)),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT)
    assert scores.low_oi_anomaly_count == 1


# ─── Cas 5 : wall proche + volume élevé + DTE court → ACTIONABLE ─────────────

def test_wall_near_high_volume_short_dte_actionable():
    """Wall proche spot, volume fort, DTE court → profil ACTIONABLE (mode OI)."""
    # Strike à 1% du spot → prox ≈ 0.9
    # Expiry "03JUN26" (DTE=3 depuis 2026-05-31) → urgence = 0.8
    # OI=500, volume=400 → flow=0.8, active_pct=80%
    # actionable_pct ≈ prox × dte_urg × 100 = 0.9 × 0.8 × 100 = 72% → ACTIONABLE
    near_strike = BTC_SPOT * 1.01
    opts = [
        _opt(near_strike, "call", oi=500, volume=400.0, expiry="03JUN26"),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=False)
    assert scores.profile == TAG_ACTIONABLE, (
        f"Attendu ACTIONABLE, obtenu {scores.profile} "
        f"(active_pct={scores.active_pct:.1f}%, actionable_pct={scores.actionable_pct:.1f}%)"
    )


# ─── Cas 6 : wall lointain + volume faible → DORMANT ─────────────────────────

def test_wall_far_no_volume_dormant():
    """Wall à >10% du spot, volume=0 → DORMANT."""
    far_strike = BTC_SPOT * 1.15   # prox = 0
    opts = [
        _opt(far_strike, "call", oi=3000, volume=0.0),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=False)
    assert scores.profile == TAG_DORMANT, (
        f"Attendu DORMANT, obtenu {scores.profile}"
    )


def test_wall_far_with_volume_structural_not_actionable():
    """Wall loin + volume présent → STRUCTURAL (prox=0 → actionable=0 → pas ACTIONABLE)."""
    far_strike = BTC_SPOT * 1.15   # prox = 0.0
    opts = [
        _opt(far_strike, "call", oi=500, volume=400.0, expiry="02JUN30"),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=False)
    # active_pct > 5% (volume présent), mais actionable = 0 (prox = 0)
    # → STRUCTURAL
    assert scores.actionable == 0.0
    assert scores.profile == TAG_STRUCTURAL, (
        f"Attendu STRUCTURAL, obtenu {scores.profile}"
    )


# ─── Hiérarchie structural ≥ active ≥ actionable ─────────────────────────────

def test_hierarchy_structural_geq_active_geq_actionable():
    """En valeur absolue : |structural| ≥ |active| ≥ |actionable|."""
    opts = [
        _opt(BTC_SPOT,        "call", oi=1000, volume=500.0, delta=0.7),
        _opt(BTC_SPOT - 2000, "put",  oi=800,  volume=200.0, delta=-0.3),
        _opt(BTC_SPOT + 5000, "call", oi=300,  volume=50.0,  delta=0.3),
    ]
    scores = compute_structural_active_actionable(opts, BTC_SPOT)
    assert abs(scores.structural) >= abs(scores.active) - 0.001
    assert abs(scores.active) >= abs(scores.actionable) - 0.001


# ─── Snapshot vide ───────────────────────────────────────────────────────────

def test_empty_snapshot():
    scores = compute_structural_active_actionable([], BTC_SPOT)
    assert scores.structural == 0.0
    assert scores.active == 0.0
    assert scores.actionable == 0.0
    assert scores.profile == TAG_DORMANT
    assert scores.low_oi_anomaly_count == 0


# ─── Cohérence avec compute_dex_levels (non-régression) ──────────────────────

def test_engine_matches_dex_levels_structural():
    """L'engine doit produire le même structural que compute_dex_levels."""
    from backend.dealer_pressure import compute_dex_levels

    opts = [
        _opt(BTC_SPOT, "call", oi=500, volume=200.0, delta=0.6),
        _opt(BTC_SPOT, "put",  oi=400, volume=50.0,  delta=-0.4),
    ]
    snap = _snap(opts)
    dex = compute_dex_levels(snap)
    engine = compute_structural_active_actionable(opts, BTC_SPOT, use_dealer_delta=True)

    assert abs(dex.structural - engine.structural) < 0.01
    assert abs(dex.active - engine.active) < 0.01
    assert abs(dex.actionable - engine.actionable) < 0.01
