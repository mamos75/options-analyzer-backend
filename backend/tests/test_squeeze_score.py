"""
Tests de non-régression — squeeze_score.py Signal 1 (GEX Polarity).

Après audit gamma effectif (2026-06-01) :
  Signal 1 utilise gex_near (near-term pondéré DTE×distance×delta×activité).
  Seuils ~10x plus petits que total_gex (magnitude near-term réduite).
  Nouveaux seuils near-term : -300M=critique, -50M=fort, -10M=léger, ±10M=neutre.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dataclasses import dataclass, field
from typing import List

from backend.deribit_client import OptionData, MarketSnapshot
from backend.squeeze_score import compute_squeeze_score, SqueezeScore


BTC_SPOT = 70_000.0


# ── Mocks minimaux ───────────────────────────────────────────────────────────

@dataclass
class _GEXProfile:
    total_gex: float
    gex_near: float = 0.0   # Signal 1 et direction_bias utilisent gex_near
    flip_level: float = 65_000.0
    gamma_walls: List[float] = field(default_factory=list)
    regime: str = "NEUTRE"


@dataclass
class _DealerPressure:
    net_delta: float = 0.0
    net_delta_usd: float = 0.0
    pressure_pct: float = 0.0
    direction: str = "NEUTRAL"
    intensity: str = "FAIBLE"
    gauge_color: str = "gray"
    flux_conditionnel: str = ""
    direction_risque_trader: str = "NEUTRE"
    exposition_nette_btc: float = 0.0


def _minimal_snapshot() -> MarketSnapshot:
    opt = OptionData(
        instrument="BTC-30MAY26-70000-C",
        strike=BTC_SPOT,
        expiry="30MAY26",
        option_type="call",
        oi=100.0,
        volume=5.0,
        gamma=0.0001,
        delta=0.5,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )
    return MarketSnapshot(btc_price=BTC_SPOT, options=[opt], timestamp=0.0)


def _squeeze(gex_near_value: float) -> SqueezeScore:
    """Teste le Signal 1 via gex_near — seule valeur utilisée pour le squeeze near-term."""
    gex_profile = _GEXProfile(
        total_gex=gex_near_value,
        gex_near=gex_near_value,
        regime="AMPLIFICATEUR" if gex_near_value < 0 else "STABILISANT",
    )
    return compute_squeeze_score(
        snapshot=_minimal_snapshot(),
        gex_profile=gex_profile,
        dealer_pressure=_DealerPressure(),
        iv_rank=50.0,
    )


def _s1_score(gex_value: float) -> float:
    """Extrait le score du Signal 1 (Effet Market Makers)."""
    sq = _squeeze(gex_value)
    s1 = next(s for s in sq.signals if s.name == "Effet Market Makers")
    return s1.score


# ── Tests calibration near-term (seuils post-audit gamma effectif 2026-06-01) ─

def test_gex_near_minus_8M_is_neutral():
    """gex_near -8M : entre ±10M → neutre (s1=50)."""
    score = _s1_score(-8_000_000)
    assert score == 50.0, f"gex_near -8M doit donner s1=50 (neutre), got {score}"


def test_gex_near_minus_50M_is_strong_amplification():
    """gex_near -50M : ≤ -50M → amplification forte (s1=80)."""
    score = _s1_score(-50_000_000)
    assert score == 80.0, f"gex_near -50M doit donner s1=80, got {score}"


def test_gex_near_minus_500M_is_critical():
    """gex_near -500M : ≤ -300M → critique (s1=95)."""
    score = _s1_score(-500_000_000)
    assert score == 95.0, f"gex_near -500M doit donner s1=95 (critique), got {score}"


def test_gex_near_minus_3B_is_critical():
    """gex_near -3B : ≤ -300M → critique (s1=95)."""
    score = _s1_score(-3_000_000_000)
    assert score == 95.0, f"gex_near -3B doit donner s1=95, got {score}"


def test_gex_near_plus_20M_is_stabilizing():
    """gex_near +20M : 10M ≤ gex < 50M → stabilisant modéré (s1=30)."""
    score = _s1_score(20_000_000)
    assert score == 30.0, f"gex_near +20M doit donner s1=30, got {score}"


def test_gex_near_plus_200M_is_strong_compression():
    """gex_near +200M : ≥ 50M → compression forte (s1=10)."""
    score = _s1_score(200_000_000)
    assert score == 10.0, f"gex_near +200M doit donner s1=10, got {score}"


# ── Régression : hiérarchie des seuils near-term ─────────────────────────────

def test_regression_old_millions_threshold():
    """
    Post-audit gamma effectif : la hiérarchie near-term est correcte.
    -8M=neutre < -50M=fort < -500M=critique.
    Far-term OI massif ne pollue plus le signal 1.
    """
    score_neutral = _s1_score(-8_000_000)
    score_strong  = _s1_score(-50_000_000)
    score_critical = _s1_score(-500_000_000)

    assert score_neutral < score_strong, (
        f"8M doit être < 50M en sévérité: {score_neutral} vs {score_strong}"
    )
    assert score_strong < score_critical, (
        f"50M doit être < 500M en sévérité: {score_strong} vs {score_critical}"
    )
    assert score_neutral == 50.0, (
        f"gex_near -8M doit être neutre (s1=50): got {score_neutral}"
    )


# ── Runner standalone ─────────────────────────────────────────────────────────

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
        sys.exit(1)
