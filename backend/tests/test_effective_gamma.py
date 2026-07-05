"""
Tests — compute_effective_gamma_horizons() et propagation dans GEXProfile.

Règle gravée :
  Near-term gamma effectif = seul signal valide pour alertes / scores actionnables.
  Far-term OI ne doit jamais piloter un score ou une alerte.
"""

import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.deribit_client import OptionData, MarketSnapshot
from backend.gex import compute_gex, compute_effective_gamma_horizons, GEXProfile

BTC_SPOT = 75_000.0


def _expiry_dte(dte: int) -> str:
    d = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%d%b%y").upper()
    return d


def _opt(strike, opt_type, gamma=0.00003, oi=1000, volume=100, delta=0.5, dte=7):
    expiry = _expiry_dte(dte)
    return OptionData(
        instrument=f"BTC-{expiry}-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry=expiry,
        option_type=opt_type,
        oi=oi,
        volume=volume,
        gamma=gamma,
        delta=delta,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )


def _snapshot(options):
    return MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)


# ── Test 1 : near-term domine far-term à gamma/OI égaux ─────────────────────

def test_near_dominates_far_at_equal_oi():
    """Une option DTE=7 contribue plus au gex_near qu'une option DTE=90 au gex_global."""
    opt_near = _opt(BTC_SPOT, "call", gamma=0.00003, oi=1000, dte=7)
    opt_far  = _opt(BTC_SPOT, "call", gamma=0.00003, oi=1000, dte=90)
    snap = _snapshot([opt_near, opt_far])
    h = compute_effective_gamma_horizons(snap)
    assert h.near > 0, "gex_near doit être positif (calls dominants near)"
    assert h.global_ > 0, "gex_global doit être positif (calls dominants far)"
    # Near-term dte_weight (0.5 pour DTE=7) > far-term (0.05 pour DTE=90)
    assert h.near > h.global_, (
        f"near ({h.near:.0f}) doit dépasser global ({h.global_:.0f}) — "
        f"urgence DTE=7 >> DTE=90"
    )


# ── Test 2 : far-term seul → gex_near = 0 ───────────────────────────────────

def test_far_term_only_gives_zero_near():
    """Options DTE > 45 → gex_near = 0, gex_global > 0."""
    opt_far = _opt(BTC_SPOT, "call", gamma=0.00001, oi=5000, dte=60)
    snap = _snapshot([opt_far])
    h = compute_effective_gamma_horizons(snap)
    assert h.near == 0.0, f"gex_near doit être 0 sans options near-term, got {h.near}"
    assert h.global_ > 0, "gex_global doit être positif"


# ── Test 3 : near-term seul → gex_global = gex_monthly = 0 ─────────────────

def test_near_term_only_gives_zero_global():
    """Options DTE ≤ 14 → gex_global = 0, gex_monthly = 0."""
    opt_near = _opt(BTC_SPOT, "call", gamma=0.00003, oi=1000, dte=5)
    snap = _snapshot([opt_near])
    h = compute_effective_gamma_horizons(snap)
    assert h.global_ == 0.0, "gex_global doit être 0 sans options far-term"
    assert h.monthly == 0.0, "gex_monthly doit être 0 sans options mid-term"
    assert h.near != 0.0, "gex_near doit être non-nul"


# ── Test 4 : distance_weight — option ATM > option OTM ──────────────────────

def test_atm_outweighs_otm_at_equal_oi():
    """Option ATM (strike=spot) contribue plus qu'OTM (strike=spot×1.15), même OI."""
    opt_atm = _opt(BTC_SPOT, "call", gamma=0.00003, oi=1000, dte=7)
    opt_otm = _opt(BTC_SPOT * 1.15, "call", gamma=0.00003, oi=1000, dte=7)
    snap_atm = _snapshot([opt_atm])
    snap_otm = _snapshot([opt_otm])
    h_atm = compute_effective_gamma_horizons(snap_atm)
    h_otm = compute_effective_gamma_horizons(snap_otm)
    # OTM à 15% est hors de la fenêtre proximity (max 10%) → prox=0 → near_otm = 0 via activity_weight min
    assert abs(h_atm.near) > abs(h_otm.near), (
        f"ATM ({h_atm.near:.2f}) doit contribuer plus qu'OTM à 15% ({h_otm.near:.2f})"
    )


# ── Test 5 : signe correct — puts rendent gex_near négatif ──────────────────

def test_puts_produce_negative_gex_near():
    """Puts dominants → gex_near < 0."""
    opt_put = _opt(BTC_SPOT, "put", gamma=0.00003, oi=1000, dte=7, delta=-0.5)
    snap = _snapshot([opt_put])
    h = compute_effective_gamma_horizons(snap)
    assert h.near < 0, f"Puts → gex_near doit être négatif, got {h.near}"


# ── Test 6 : GEXProfile expose bien les horizons ─────────────────────────────

def test_gex_profile_includes_horizons():
    """compute_gex() peuple gex_near / gex_monthly / gex_global dans GEXProfile."""
    options = [
        _opt(BTC_SPOT, "call", oi=500, dte=7),
        _opt(BTC_SPOT, "call", oi=500, dte=30),
        _opt(BTC_SPOT, "call", oi=500, dte=90),
    ]
    snap = _snapshot(options)
    profile = compute_gex(snap)
    assert isinstance(profile.gex_near, float), "gex_near doit être un float"
    assert isinstance(profile.gex_monthly, float), "gex_monthly doit être un float"
    assert isinstance(profile.gex_global, float), "gex_global doit être un float"
    # Tous positifs (calls dominants)
    assert profile.gex_near > 0
    assert profile.gex_monthly > 0
    assert profile.gex_global > 0


# ── Test 7 : massive far-term OI n'écrase pas near ──────────────────────────

def test_massive_far_term_does_not_inflate_near():
    """10x plus d'OI far-term ne doit pas polluer gex_near."""
    opt_near = _opt(BTC_SPOT, "call", oi=100, dte=5)
    opt_far  = _opt(BTC_SPOT, "call", oi=100_000, dte=90)  # 1000x OI far
    snap_near_only = _snapshot([opt_near])
    snap_combined  = _snapshot([opt_near, opt_far])
    h_near = compute_effective_gamma_horizons(snap_near_only)
    h_comb = compute_effective_gamma_horizons(snap_combined)
    # gex_near doit être identique — far-term OI ne pollue pas near
    assert abs(h_near.near - h_comb.near) < 1e-6, (
        f"Far-term OI (×1000) ne doit pas modifier gex_near. "
        f"Near only: {h_near.near:.2f}, Combined: {h_comb.near:.2f}"
    )


if __name__ == "__main__":
    tests = [
        test_near_dominates_far_at_equal_oi,
        test_far_term_only_gives_zero_near,
        test_near_term_only_gives_zero_global,
        test_atm_outweighs_otm_at_equal_oi,
        test_puts_produce_negative_gex_near,
        test_gex_profile_includes_horizons,
        test_massive_far_term_does_not_inflate_near,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
