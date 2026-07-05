"""
Tests de non-régression — DEX 3 niveaux (Structural / Active / Actionable).

Règle fondamentale :
  DEX = delta × OI × contract_size  (pas gamma × OI × spot²)
  Gamma → GEX. Delta → DEX. Ne jamais mélanger.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.deribit_client import OptionData, MarketSnapshot
from backend.dealer_pressure import (
    compute_dex_levels,
    _proximity,
    _dte_urgency,
    _compute_flow_ratio,
    CONTRACT_SIZE,
    _MIN_OI_FLOW,
    _FLOW_RATIO_CAP,
    _LOW_OI_ANOMALY_OI,
)

BTC_SPOT = 94_000.0
EXPIRY_NEAR = "30JAN30"   # DTE > 0 quelle que soit la date d'exécution
EXPIRY_3D   = "03JUN30"   # simulé DTE ~ 3 dans les tests de structure — valeur réelle importera peu


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


def _snapshot(options):
    return MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)


# ─── Formule : delta × OI × contract_size (pas gamma × spot²) ────────────────

def test_structural_dex_formula():
    """Structural DEX = Σ(dealer_delta × OI × CONTRACT_SIZE), pas de gamma ni spot²."""
    # 1 call : delta=0.6, OI=100 → dealer_delta = -0.6 → structural = -0.6 × 100 × 1 = -60
    opt = _opt(BTC_SPOT, "call", oi=100, delta=0.6)
    dex = compute_dex_levels(_snapshot([opt]))
    assert abs(dex.structural - (-60.0)) < 0.01, f"Expected -60, got {dex.structural}"


def test_structural_dex_put():
    """Put : delta négatif → dealer_delta positif → structural > 0."""
    # delta_put = -0.4 → dealer_delta = +0.4 → structural = +0.4 × 100 × 1 = +40
    opt = _opt(BTC_SPOT, "put", oi=100, delta=-0.4)
    dex = compute_dex_levels(_snapshot([opt]))
    assert abs(dex.structural - 40.0) < 0.01, f"Expected +40, got {dex.structural}"


def test_structural_dex_usd():
    """Structural USD = structural × spot."""
    opt = _opt(BTC_SPOT, "call", oi=100, delta=0.6)
    dex = compute_dex_levels(_snapshot([opt]))
    expected_usd = dex.structural * BTC_SPOT
    assert abs(dex.structural_usd - expected_usd) < 1.0


def test_structural_uses_contract_size():
    """Vérifier que CONTRACT_SIZE est bien intégré (= 1 sur Deribit, mais la variable existe)."""
    assert CONTRACT_SIZE == 1.0


# ─── Flow ratio — garde-fous ──────────────────────────────────────────────────

def test_flow_ratio_min_oi_guard():
    """OI < _MIN_OI_FLOW → flow_ratio = 0 (pas de signal)."""
    opt = _opt(BTC_SPOT, "call", oi=_MIN_OI_FLOW - 1, volume=100.0)
    ratio, anomaly = _compute_flow_ratio(opt)
    assert ratio == 0.0
    assert anomaly is False


def test_flow_ratio_capped_at_1():
    """Volume/OI > 1 → cappé à 1.0."""
    opt = _opt(BTC_SPOT, "call", oi=1000, volume=5000.0)
    ratio, _ = _compute_flow_ratio(opt)
    assert ratio == _FLOW_RATIO_CAP == 1.0


def test_flow_ratio_normal():
    """Volume/OI = 0.5 → flow_ratio = 0.5."""
    opt = _opt(BTC_SPOT, "call", oi=200, volume=100.0)
    ratio, anomaly = _compute_flow_ratio(opt)
    assert abs(ratio - 0.5) < 0.001
    assert anomaly is False


def test_flow_ratio_low_oi_anomaly():
    """OI faible + volume > OI → tag low_oi_anomaly = True."""
    opt = _opt(BTC_SPOT, "call", oi=_LOW_OI_ANOMALY_OI - 1, volume=float(_LOW_OI_ANOMALY_OI))
    ratio, anomaly = _compute_flow_ratio(opt)
    assert ratio == _FLOW_RATIO_CAP
    assert anomaly is True


def test_low_oi_anomaly_count_in_dex():
    """compute_dex_levels comptabilise les anomalies low_oi."""
    anomaly_opt = _opt(BTC_SPOT, "call", oi=_LOW_OI_ANOMALY_OI - 1, volume=float(_LOW_OI_ANOMALY_OI))
    normal_opt  = _opt(BTC_SPOT + 1000, "call", oi=500, volume=100.0)
    dex = compute_dex_levels(_snapshot([anomaly_opt, normal_opt]))
    assert dex.low_oi_anomaly_count == 1
    assert BTC_SPOT in dex.low_oi_anomaly_strikes


# ─── Active DEX ───────────────────────────────────────────────────────────────

def test_active_dex_zero_when_no_volume():
    """Sans volume, flow_ratio = 0 → Active DEX = 0."""
    opts = [
        _opt(BTC_SPOT, "call", oi=500, volume=0.0),
        _opt(BTC_SPOT, "put",  oi=500, volume=0.0),
    ]
    dex = compute_dex_levels(_snapshot(opts))
    assert dex.active == 0.0


def test_active_dex_less_than_structural():
    """Active DEX ≤ |Structural DEX| en valeur absolue (flow_ratio ∈ [0,1])."""
    opts = [_opt(BTC_SPOT, "call", oi=500, volume=200.0, delta=0.6)]
    dex = compute_dex_levels(_snapshot(opts))
    assert abs(dex.active) <= abs(dex.structural)


# ─── Actionable DEX ───────────────────────────────────────────────────────────

def test_actionable_dex_zero_no_volume():
    """Sans volume → flow_ratio = 0 → Actionable = 0."""
    opt = _opt(BTC_SPOT, "call", oi=500, volume=0.0)
    dex = compute_dex_levels(_snapshot([opt]))
    assert dex.actionable == 0.0


def test_actionable_less_than_active():
    """Actionable ≤ |Active| (proximity × dte_urgency ∈ [0,1])."""
    opt = _opt(BTC_SPOT, "call", oi=500, volume=300.0, delta=0.6)
    dex = compute_dex_levels(_snapshot([opt]))
    assert abs(dex.actionable) <= abs(dex.active) + 0.001


def test_proximity_atm():
    """Strike = spot → proximity = 1.0."""
    assert _proximity(BTC_SPOT, BTC_SPOT) == 1.0


def test_proximity_10pct():
    """Strike à 10% du spot → proximity = 0.0."""
    assert _proximity(BTC_SPOT * 1.10, BTC_SPOT) == 0.0


def test_proximity_5pct():
    """Strike à 5% → proximity = 0.5."""
    val = _proximity(BTC_SPOT * 1.05, BTC_SPOT)
    assert abs(val - 0.5) < 0.001


def test_proximity_beyond_10pct():
    """Strike > 10% → proximity = 0.0 (pas négatif)."""
    assert _proximity(BTC_SPOT * 1.20, BTC_SPOT) == 0.0


# ─── DTE urgency ──────────────────────────────────────────────────────────────

def test_dte_urgency_expired():
    assert _dte_urgency(0) == 0.0


def test_dte_urgency_day1():
    assert _dte_urgency(1) == 1.0


def test_dte_urgency_dte3():
    assert _dte_urgency(3) == 0.8


def test_dte_urgency_dte7():
    assert _dte_urgency(7) == 0.5


def test_dte_urgency_dte14():
    assert _dte_urgency(14) == 0.3


def test_dte_urgency_far():
    assert _dte_urgency(90) == 0.05


# ─── Hiérarchie Structural > Active > Actionable ─────────────────────────────

def test_structural_geq_active_geq_actionable():
    """Structural ≥ Active ≥ Actionable en valeur absolue."""
    opts = [
        _opt(BTC_SPOT,         "call", oi=1000, volume=500.0, delta=0.7),
        _opt(BTC_SPOT - 2000,  "put",  oi=800,  volume=200.0, delta=-0.3),
        _opt(BTC_SPOT + 5000,  "call", oi=300,  volume=50.0,  delta=0.3),
    ]
    dex = compute_dex_levels(_snapshot(opts))
    assert abs(dex.structural) >= abs(dex.active) - 0.001
    assert abs(dex.active) >= abs(dex.actionable) - 0.001


# ─── Zéro options ─────────────────────────────────────────────────────────────

def test_empty_snapshot():
    dex = compute_dex_levels(_snapshot([]))
    assert dex.structural == 0.0
    assert dex.active == 0.0
    assert dex.actionable == 0.0
    assert dex.low_oi_anomaly_count == 0
