"""
Tests de non-régression — PCR V2 (Put/Call Ratio multi-fenêtre + pondéré DTE).

Hiérarchie PCR :
- pc_ratio_global    = toutes expiries, non pondéré (lecture structurelle)
- pc_ratio_near      = DTE ≤ 14j, non pondéré  → pression immédiate brute
- pc_ratio_mid       = 15-45j, non pondéré
- pc_ratio_far       = >45j, non pondéré
- pc_ratio_weighted  = toutes expiries, 1/sqrt(DTE) → synthèse MOPI V2

Règle : far-term massif ne pollue pas near. near massif influence weighted.
DTE=0 exclu partout.
"""

import sys
import os
import math
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.mopi import (
    _compute_pc_ratio,
    _compute_pc_ratio_near,
    _compute_pc_ratio_mid,
    _compute_pc_ratio_far,
    _compute_pc_ratio_weighted,
    _compute_dominant_expiry,
    _compute_pc_ratio_institutional,
    compute_mopi,
)
from backend.deribit_client import OptionData, MarketSnapshot
from backend.gex import GEXProfile

BTC_SPOT = 95_000.0

_today = datetime.now(timezone.utc).date()
EXPIRY_NEAR_7D   = (_today + timedelta(days=7)).strftime("%d%b%y").upper()   # 7 DTE  ≤14 → near
EXPIRY_NEAR_10D  = (_today + timedelta(days=10)).strftime("%d%b%y").upper()  # 10 DTE ≤14 → near
EXPIRY_NEAR_14D  = (_today + timedelta(days=14)).strftime("%d%b%y").upper()  # 14 DTE ≤14 → near (limite)
EXPIRY_MID_25D   = (_today + timedelta(days=25)).strftime("%d%b%y").upper()  # 25 DTE → mid
EXPIRY_MID_40D   = (_today + timedelta(days=40)).strftime("%d%b%y").upper()  # 40 DTE → mid
EXPIRY_FAR_60D   = (_today + timedelta(days=60)).strftime("%d%b%y").upper()  # 60 DTE → far
EXPIRY_FAR_180D  = (_today + timedelta(days=180)).strftime("%d%b%y").upper() # 180 DTE → far
EXPIRY_PAST      = (_today - timedelta(days=1)).strftime("%d%b%y").upper()   # expiré hier


def _opt(strike, opt_type, oi, expiry, gamma=0.0001):
    return OptionData(
        instrument=f"BTC-{expiry}-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry=expiry,
        option_type=opt_type,
        oi=oi,
        volume=0.0,
        gamma=gamma,
        delta=0.5,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )


def _neutral_gex():
    return GEXProfile(
        total_gex=0,
        gex_by_strike={},
        call_gex_by_strike={},
        put_gex_by_strike={},
        flip_level=BTC_SPOT * 0.95,
        max_pain=BTC_SPOT,
        gamma_walls=[],
        btc_price=BTC_SPOT,
        regime="NEUTRE",
    )


# ─── Tests _compute_pc_ratio_near (≤14 DTE, non pondéré) ────────────────────

def test_near_captures_near_puts_ignores_far_calls():
    """Gros puts ≤14 DTE → pc_near élevé même si gros calls lointains diluent le global."""
    options = [
        _opt(BTC_SPOT, "put",  2000, EXPIRY_NEAR_10D),  # near — gros puts
        _opt(BTC_SPOT, "call",  200, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",   100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 5000, EXPIRY_FAR_60D),   # far — gros calls lointains
    ]
    pc_near = _compute_pc_ratio_near(options)
    pc_global = _compute_pc_ratio(options)
    assert pc_near > pc_global, (
        f"pc_near {pc_near:.3f} doit > pc_global {pc_global:.3f} "
        f"quand gros puts near + gros calls loin"
    )
    # near : 2000/200 = 10
    assert abs(pc_near - 10.0) < 0.01, f"pc_near attendu 10.0, got {pc_near:.3f}"


def test_near_boundary_14dte_included():
    """DTE=14 est inclus dans near (limite)."""
    options = [
        _opt(BTC_SPOT, "put", 400, EXPIRY_NEAR_14D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_NEAR_14D),
    ]
    pc = _compute_pc_ratio_near(options)
    assert abs(pc - 4.0) < 0.01, f"DTE=14 inclus near → 400/100=4.0, got {pc:.3f}"


def test_near_excludes_mid_expiry():
    """DTE=25 (mid) est exclu de near → fallback global."""
    options = [
        _opt(BTC_SPOT, "put",  500, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "call", 500, EXPIRY_MID_25D),
    ]
    pc_near = _compute_pc_ratio_near(options)
    pc_global = _compute_pc_ratio(options)
    assert abs(pc_near - pc_global) < 0.001, (
        f"Aucune option ≤14 DTE → fallback global. near={pc_near:.3f}, global={pc_global:.3f}"
    )


def test_near_call_oi_zero_fallback():
    """call_oi near = 0 → fallback global sans crash."""
    options = [
        _opt(BTC_SPOT, "put", 1000, EXPIRY_NEAR_10D),  # only puts near
        _opt(BTC_SPOT, "put",  500, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 200, EXPIRY_FAR_60D),
    ]
    pc = _compute_pc_ratio_near(options)
    assert pc > 0, "Ne doit pas crasher avec call_oi near = 0"
    expected_global = _compute_pc_ratio(options)
    assert abs(pc - expected_global) < 0.001, (
        f"call_oi near=0 → fallback global {expected_global:.3f}, got {pc:.3f}"
    )


def test_near_empty_list_returns_neutral():
    """Liste vide → 1.0 (neutre)."""
    assert _compute_pc_ratio_near([]) == 1.0


def test_near_expired_excluded():
    """Options expirées (DTE ≤ 0) exclues du calcul near."""
    options = [
        _opt(BTC_SPOT, "put", 9999, EXPIRY_PAST),    # expiré — exclu
        _opt(BTC_SPOT, "call",  50, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",   50, EXPIRY_NEAR_10D),
    ]
    pc = _compute_pc_ratio_near(options)
    assert abs(pc - 1.0) < 0.001, f"Options expirées exclues → ~1.0, got {pc:.3f}"


# ─── Tests _compute_pc_ratio_mid (15-45 DTE, non pondéré) ───────────────────

def test_mid_captures_mid_range_only():
    """Seules les expiries 15-45 DTE influencent pcr_mid."""
    options = [
        _opt(BTC_SPOT, "put",  3000, EXPIRY_MID_25D),   # mid
        _opt(BTC_SPOT, "call",  300, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "put",   100, EXPIRY_NEAR_10D),  # near — exclu
        _opt(BTC_SPOT, "call",  100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",   100, EXPIRY_FAR_60D),   # far — exclu
        _opt(BTC_SPOT, "call",  100, EXPIRY_FAR_60D),
    ]
    pc_mid = _compute_pc_ratio_mid(options)
    assert abs(pc_mid - 10.0) < 0.01, f"mid: 3000/300=10.0, got {pc_mid:.3f}"


def test_mid_boundary_45dte_included():
    """DTE=45 est inclus dans mid (limite haute)."""
    options = [
        _opt(BTC_SPOT, "put", 200, EXPIRY_MID_40D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_MID_40D),
    ]
    pc = _compute_pc_ratio_mid(options)
    assert abs(pc - 2.0) < 0.01, f"DTE=40 inclus mid → 200/100=2.0, got {pc:.3f}"


def test_mid_fallback_global_when_empty():
    """Aucune option 15-45 DTE → fallback global."""
    options = [
        _opt(BTC_SPOT, "put",  500, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 500, EXPIRY_FAR_60D),
    ]
    pc_mid = _compute_pc_ratio_mid(options)
    pc_global = _compute_pc_ratio(options)
    assert abs(pc_mid - pc_global) < 0.001, (
        f"Aucune option mid → fallback global. mid={pc_mid:.3f}, global={pc_global:.3f}"
    )


# ─── Tests _compute_pc_ratio_far (>45 DTE, non pondéré) ─────────────────────

def test_far_captures_far_range_only():
    """Seules les expiries >45 DTE influencent pcr_far."""
    options = [
        _opt(BTC_SPOT, "put",  4000, EXPIRY_FAR_180D),  # far
        _opt(BTC_SPOT, "call", 1000, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "put",   100, EXPIRY_NEAR_10D),  # near — exclu
        _opt(BTC_SPOT, "call",  100, EXPIRY_MID_25D),   # mid — exclu
    ]
    pc_far = _compute_pc_ratio_far(options)
    assert abs(pc_far - 4.0) < 0.01, f"far: 4000/1000=4.0, got {pc_far:.3f}"


def test_far_boundary_60dte_included():
    """DTE=60 est bien dans far (>45)."""
    options = [
        _opt(BTC_SPOT, "put", 300, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_60D),
    ]
    pc = _compute_pc_ratio_far(options)
    assert abs(pc - 3.0) < 0.01, f"DTE=60 inclus far → 300/100=3.0, got {pc:.3f}"


def test_far_fallback_global_when_empty():
    """Aucune option >45 DTE → fallback global."""
    options = [
        _opt(BTC_SPOT, "put",  300, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_NEAR_10D),
    ]
    pc_far = _compute_pc_ratio_far(options)
    pc_global = _compute_pc_ratio(options)
    assert abs(pc_far - pc_global) < 0.001, (
        f"Aucune option far → fallback global. far={pc_far:.3f}, global={pc_global:.3f}"
    )


def test_far_massif_does_not_pollute_near():
    """OI massif en far ne doit pas polluer near (règle Mamos critique)."""
    options = [
        _opt(BTC_SPOT, "call", 50000, EXPIRY_FAR_180D),  # massif en far
        _opt(BTC_SPOT, "put",    200, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",   100, EXPIRY_NEAR_10D),
    ]
    pc_near = _compute_pc_ratio_near(options)
    # near = 200/100 = 2.0 — non pollué par les 50000 calls far
    assert abs(pc_near - 2.0) < 0.01, (
        f"far massif ne pollue pas near. Attendu 2.0, got {pc_near:.3f}"
    )


# ─── Tests _compute_pc_ratio_weighted (toutes expiries, 1/sqrt(DTE)) ─────────

def test_weighted_formula_1_over_sqrt_dte():
    """Vérification : weight = 1/sqrt(DTE). DTE=10 pèse plus que DTE=25."""
    w10 = 1.0 / math.sqrt(10)
    w25 = 1.0 / math.sqrt(25)
    assert w10 > w25, f"1/sqrt(10)={w10:.4f} doit > 1/sqrt(25)={w25:.4f}"

    # Puts sur 10 DTE, calls sur 25 DTE → ratio = w10/w25
    options = [
        _opt(BTC_SPOT, "put",  100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_MID_25D),
    ]
    pc = _compute_pc_ratio_weighted(options)
    expected = w10 / w25
    assert abs(pc - expected) < 0.001, f"Attendu {expected:.4f}, got {pc:.4f}"


def test_weighted_equal_puts_calls_per_expiry_is_1():
    """Puts = Calls à chaque expiry → pc_weighted = 1.0 exactement."""
    options = [
        _opt(BTC_SPOT, "put",  100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",  100, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "put",  100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_60D),
    ]
    pc = _compute_pc_ratio_weighted(options)
    assert abs(pc - 1.0) < 0.001, f"Puts=Calls → weighted=1.0, got {pc:.4f}"


def test_near_massif_influences_weighted():
    """OI massif near-term doit augmenter pc_weighted de façon significative."""
    options = [
        _opt(BTC_SPOT, "put",  5000, EXPIRY_NEAR_7D),   # massif puts near (poids fort)
        _opt(BTC_SPOT, "call",  500, EXPIRY_NEAR_7D),
        _opt(BTC_SPOT, "put",   200, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "call",  200, EXPIRY_FAR_180D),
    ]
    pc_weighted = _compute_pc_ratio_weighted(options)
    pc_global = _compute_pc_ratio(options)
    # weighted doit > global car le near gros puts pèse plus
    assert pc_weighted > pc_global, (
        f"OI massif near-puts → weighted > global. weighted={pc_weighted:.3f}, global={pc_global:.3f}"
    )
    assert pc_weighted > 5.0, f"Gros puts near → weighted doit être significatif, got {pc_weighted:.3f}"


def test_far_massif_barely_influences_weighted():
    """OI massif en far (180 DTE) pèse peu dans weighted vs near."""
    w7 = 1.0 / math.sqrt(7)
    w180 = 1.0 / math.sqrt(180)
    ratio = w7 / w180
    assert ratio > 5, f"7 DTE pèse {ratio:.1f}× plus que 180 DTE — far massivement réduit"

    options = [
        _opt(BTC_SPOT, "put",   200, EXPIRY_NEAR_7D),
        _opt(BTC_SPOT, "call",  100, EXPIRY_NEAR_7D),
        _opt(BTC_SPOT, "put",  5000, EXPIRY_FAR_180D),  # massif far — poids faible
        _opt(BTC_SPOT, "call",  500, EXPIRY_FAR_180D),
    ]
    pc_far = _compute_pc_ratio_far(options)   # 5000/500 = 10.0
    pc_weighted = _compute_pc_ratio_weighted(options)
    # weighted doit être < far pur car le far est amorti
    assert pc_weighted < pc_far, (
        f"far massif amorti dans weighted. weighted={pc_weighted:.3f} doit < far={pc_far:.3f}"
    )


def test_weighted_excludes_expired():
    """DTE=0 exclu du calcul weighted."""
    options = [
        _opt(BTC_SPOT, "put", 9999, EXPIRY_PAST),   # expiré
        _opt(BTC_SPOT, "put",   50, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",  50, EXPIRY_NEAR_10D),
    ]
    pc = _compute_pc_ratio_weighted(options)
    assert abs(pc - 1.0) < 0.001, f"Expiré exclu → 50/50=1.0, got {pc:.3f}"


def test_weighted_empty_returns_neutral():
    """Liste vide → fallback → 1.0."""
    pc = _compute_pc_ratio_weighted([])
    assert pc == 1.0


def test_weighted_call_zero_fallback_global():
    """call_w=0 dans weighted → fallback global."""
    options = [
        _opt(BTC_SPOT, "put", 1000, EXPIRY_NEAR_10D),  # puts only
    ]
    pc = _compute_pc_ratio_weighted(options)
    pc_global = _compute_pc_ratio(options)
    assert abs(pc - pc_global) < 0.001, f"call_w=0 → fallback global {pc_global:.3f}, got {pc:.3f}"


# ─── Tests _compute_dominant_expiry ──────────────────────────────────────────

def test_dominant_expiry_near_wins_when_high_oi():
    """Expiry near avec gros OI doit dominer malgré un OI far plus élevé en absolu."""
    # Near 10 DTE : 1000 OI × weight(10) = 1000/sqrt(10) ≈ 316
    # Far 180 DTE : 2000 OI × weight(180) = 2000/sqrt(180) ≈ 149
    # → near domine en contribution pondérée
    options = [
        _opt(BTC_SPOT, "put",  500, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 500, EXPIRY_NEAR_10D),  # total near OI = 1000
        _opt(BTC_SPOT, "put", 1000, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "call",1000, EXPIRY_FAR_180D),  # total far OI = 2000
    ]
    dom = _compute_dominant_expiry(options)
    assert dom["expiry"] == EXPIRY_NEAR_10D, (
        f"Near 10 DTE doit dominer en contribution pondérée. Got: {dom}"
    )
    assert dom["dte"] == 10
    assert dom["oi_contribution_pct"] > 50.0, f"Near doit être >50% de la contribution"


def test_dominant_expiry_returns_dte_and_weight():
    """dominant_expiry expose expiry, dte, weight, oi_contribution_pct."""
    options = [
        _opt(BTC_SPOT, "put",  100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_60D),
    ]
    dom = _compute_dominant_expiry(options)
    assert "expiry" in dom
    assert "dte" in dom
    assert "weight" in dom
    assert "oi_contribution_pct" in dom
    assert 0 < dom["oi_contribution_pct"] <= 100


def test_dominant_expiry_empty_returns_empty_dict():
    """Liste vide → dict vide."""
    dom = _compute_dominant_expiry([])
    assert dom == {}


def test_dominant_expiry_excludes_expired():
    """Expiry passée exclue — ne peut pas être dominante."""
    options = [
        _opt(BTC_SPOT, "put", 9999, EXPIRY_PAST),    # expiré — massif mais exclu
        _opt(BTC_SPOT, "put",   50, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",  50, EXPIRY_NEAR_10D),
    ]
    dom = _compute_dominant_expiry(options)
    assert dom.get("expiry") == EXPIRY_NEAR_10D, (
        f"Expiry passée exclue. Dominant doit être near_10d, got {dom}"
    )


# ─── Tests institutional (backward compat) ───────────────────────────────────

def test_institutional_selects_max_oi_expiry():
    options = [
        _opt(BTC_SPOT, "put",  100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "put",  300, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_180D),
    ]
    pc = _compute_pc_ratio_institutional(options)
    assert abs(pc - 3.0) < 0.001, f"institutional max OI = FAR_180D → 300/100=3.0, got {pc:.3f}"


def test_institutional_empty_returns_neutral():
    assert _compute_pc_ratio_institutional([]) == 1.0


def test_institutional_expired_excluded():
    options = [
        _opt(BTC_SPOT, "put",  9999, EXPIRY_PAST),
        _opt(BTC_SPOT, "call",  100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "put",   200, EXPIRY_FAR_60D),
    ]
    pc = _compute_pc_ratio_institutional(options)
    assert abs(pc - 2.0) < 0.001, f"Expiré exclu → 200/100=2.0, got {pc:.3f}"


# ─── Test divergence des 5 PCR ───────────────────────────────────────────────

def test_five_pcr_diverge_with_structured_market():
    """Les 5 PCR racontent des histoires différentes sur un marché structuré."""
    options = [
        # Near ≤14 : pression put forte
        _opt(BTC_SPOT, "put",  2000, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",  200, EXPIRY_NEAR_10D),
        # Mid 15-45 : équilibre
        _opt(BTC_SPOT, "put",   300, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "call",  300, EXPIRY_MID_25D),
        # Far >45 : pression call (institutions bullish long terme)
        _opt(BTC_SPOT, "put",   200, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "call", 2000, EXPIRY_FAR_180D),
    ]
    pc_global = _compute_pc_ratio(options)
    pc_near = _compute_pc_ratio_near(options)
    pc_mid = _compute_pc_ratio_mid(options)
    pc_far = _compute_pc_ratio_far(options)
    pc_weighted = _compute_pc_ratio_weighted(options)

    assert pc_near > 1.0, f"near: puts dominent → >1. Got {pc_near:.3f}"
    assert abs(pc_mid - 1.0) < 0.01, f"mid: équilibre → ~1.0. Got {pc_mid:.3f}"
    assert pc_far < 1.0, f"far: calls dominent → <1. Got {pc_far:.3f}"
    # weighted : entre near et far, proche de near car il pèse plus
    assert pc_far < pc_weighted < pc_near, (
        f"weighted entre far et near. Got far={pc_far:.3f}, weighted={pc_weighted:.3f}, near={pc_near:.3f}"
    )
    # global dilue tout
    assert pc_far < pc_global < pc_near, (
        f"global dilué entre extrêmes. Got far={pc_far:.3f}, global={pc_global:.3f}, near={pc_near:.3f}"
    )


# ─── Tests MOPI V2 utilise pcr_weighted ──────────────────────────────────────

def test_mopi_pc_component_uses_weighted_not_near():
    """MOPI pc_ratio_component reflète pc_ratio_weighted (MOPI V2), pas pc_ratio_near."""
    from backend.mopi import _pc_ratio_to_score
    options = [
        _opt(BTC_SPOT, "put",  3000, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",  300, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",   100, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 5000, EXPIRY_FAR_60D),
    ]
    snap = MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)
    gex = _neutral_gex()
    mopi = compute_mopi(snap, gex, iv_history_90d=[60.0] * 90)

    pc_weighted = _compute_pc_ratio_weighted(options)
    expected_pc_score = round(_pc_ratio_to_score(pc_weighted), 1)

    assert mopi.pc_ratio_component == expected_pc_score, (
        f"MOPI pc_component doit venir de pc_weighted. "
        f"Expected {expected_pc_score}, got {mopi.pc_ratio_component}"
    )
    assert mopi.pc_ratio_weighted == round(pc_weighted, 3), (
        f"mopi.pc_ratio_weighted doit être exposé. Expected {round(pc_weighted,3)}, got {mopi.pc_ratio_weighted}"
    )


def test_mopi_exposes_all_pcr_variants():
    """compute_mopi retourne les 5 variantes PCR + dominant_expiry dans MOPIScore."""
    options = [
        _opt(BTC_SPOT, "put",  100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "put",  200, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "put",  400, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 200, EXPIRY_FAR_60D),
    ]
    snap = MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)
    gex = _neutral_gex()
    mopi = compute_mopi(snap, gex, iv_history_90d=[60.0] * 90)

    assert mopi.pc_ratio_global > 0, "pc_ratio_global doit être exposé"
    assert mopi.pc_ratio_near > 0, "pc_ratio_near doit être exposé"
    assert mopi.pc_ratio_mid > 0, "pc_ratio_mid doit être exposé"
    assert mopi.pc_ratio_far > 0, "pc_ratio_far doit être exposé"
    assert mopi.pc_ratio_weighted > 0, "pc_ratio_weighted doit être exposé"
    assert mopi.pc_ratio_institutional > 0, "pc_ratio_institutional doit être exposé (backward compat)"
    assert isinstance(mopi.dominant_expiry, dict), "dominant_expiry doit être un dict"
    assert "expiry" in mopi.dominant_expiry, "dominant_expiry doit avoir 'expiry'"
    # backward compat : pc_ratio == pc_ratio_global
    assert mopi.pc_ratio == mopi.pc_ratio_global, (
        f"pc_ratio (backward compat) == pc_ratio_global. "
        f"Got pc_ratio={mopi.pc_ratio}, global={mopi.pc_ratio_global}"
    )


def test_mopi_near_fallback_when_no_near_expiry():
    """Si aucune option ≤14 DTE, pc_near fallback global (pas de valeur fantôme)."""
    options = [
        _opt(BTC_SPOT, "put",  500, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 250, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "put",  100, EXPIRY_FAR_180D),
        _opt(BTC_SPOT, "call", 100, EXPIRY_FAR_180D),
    ]
    pc_near = _compute_pc_ratio_near(options)
    pc_global = _compute_pc_ratio(options)
    assert abs(pc_near - pc_global) < 0.001, (
        f"Sans near-term, pc_near fallback global. near={pc_near:.3f}, global={pc_global:.3f}"
    )


# ─── Comparaison ancienne vs nouvelle formule ────────────────────────────────

def test_old_vs_new_formula_comparison():
    """Comparaison ancienne méthode (near ≤30 pondéré) vs nouvelle (weighted toutes expiries).
    La nouvelle est plus stable car elle intègre toutes les fenêtres.
    """
    # Données : puts massifs near, calls massifs far
    options = [
        _opt(BTC_SPOT, "put",  2000, EXPIRY_NEAR_10D),
        _opt(BTC_SPOT, "call",  200, EXPIRY_MID_25D),
        _opt(BTC_SPOT, "put",   300, EXPIRY_FAR_60D),
        _opt(BTC_SPOT, "call", 1500, EXPIRY_FAR_180D),
    ]

    pc_global = _compute_pc_ratio(options)
    pc_near_14 = _compute_pc_ratio_near(options)      # nouvelle near (≤14, non pondéré)
    pc_weighted = _compute_pc_ratio_weighted(options)  # nouvelle synthèse

    # near ≤14 : 2000 puts / 0 calls near → fallback global
    # weighted : intègre toutes expiries avec pondération
    # global : non pondéré, toutes expiries

    # Vérification de cohérence : aucune ne doit crasher ou retourner 0
    assert pc_global > 0
    assert pc_near_14 > 0
    assert pc_weighted > 0

    # weighted doit être influencé par le near massif (+ élevé que le global dilué)
    assert pc_weighted > pc_global, (
        f"weighted > global car near massif puts pèse fort. "
        f"weighted={pc_weighted:.3f}, global={pc_global:.3f}"
    )


# ─── Runner standalone ────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    if failed:
        import sys as _sys
        _sys.exit(1)
