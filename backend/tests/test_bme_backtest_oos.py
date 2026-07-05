"""
test_bme_backtest_oos.py — Tests Phase B1 :
  - Wilson bounds et contrarian significance
  - has_edge : false a 53%/n=30, true a 65%/n=100
  - contrarian non active a 40%/n=30 (Wilson upper ~ 0.58 > 0.5)
  - contrarian active a 30%/n=100 (Wilson upper ~ 0.39 < 0.5)
"""
from __future__ import annotations
import math
import pytest
from backend.wilson_utils import (
    wilson_lower, wilson_upper, wilson_bounds, has_edge, contrarian_significant
)


# ── Wilson bounds sanity ──────────────────────────────────────────────────────

def test_wilson_bounds_basic():
    """50%/100 -> IC centre sur 0.5."""
    lb, ub = wilson_bounds(0.50, 100)
    assert lb < 0.50 < ub
    assert ub - lb < 0.20


def test_wilson_bounds_zero_n():
    lb, ub = wilson_bounds(0.60, 0)
    assert lb == 0.0
    assert ub == 1.0


def test_wilson_lower_monotone():
    """Plus n est grand, borne inferieure monte."""
    lb30  = wilson_lower(0.65, 30)
    lb100 = wilson_lower(0.65, 100)
    lb300 = wilson_lower(0.65, 300)
    assert lb30 < lb100 < lb300


# ── has_edge ──────────────────────────────────────────────────────────────────

def test_has_edge_false_low_n():
    """n < min_n -> pas d'edge meme avec WR eleve."""
    assert has_edge(0.70, 10) is False
    assert has_edge(0.80, 5)  is False


def test_has_edge_false_53pct_n30():
    """53%/n=30 -> wilson_lower < 0.50 -> pas d'edge (audit E4)."""
    # wilson_lower(0.53, 30) ~ 0.35 < 0.50
    assert has_edge(0.53, 30) is False


def test_has_edge_true_65pct_n100():
    """65%/n=100 -> wilson_lower > 0.50 -> edge detecte."""
    # wilson_lower(0.65, 100) ~ 0.554 > 0.50
    assert has_edge(0.65, 100) is True


def test_has_edge_none_wr():
    """wr=None -> pas d'edge."""
    assert has_edge(None, 100) is False


# ── contrarian_significant ────────────────────────────────────────────────────

def test_contrarian_not_activated_40pct_n30():
    """
    B1 audit : WR=40%/n=30 -> wilson_upper ~ 0.58 > 0.50
    -> contrarian NON significatif (l'ancien seuil naif etait incorrect).
    """
    ub = wilson_upper(0.40, 30)
    assert ub > 0.50, f"wilson_upper(0.40,30)={ub:.3f} devrait etre > 0.50"
    assert contrarian_significant(0.40, 30) is False


def test_contrarian_activated_30pct_n100():
    """WR=30%/n=100 -> wilson_upper < 0.50 -> contrarian ACTIVE."""
    ub = wilson_upper(0.30, 100)
    assert ub < 0.50, f"wilson_upper(0.30,100)={ub:.3f} devrait etre < 0.50"
    assert contrarian_significant(0.30, 100) is True


def test_contrarian_not_activated_low_n():
    """n < min_n -> contrarian jamais active."""
    assert contrarian_significant(0.10, 10) is False
    assert contrarian_significant(0.0,  20) is False


def test_contrarian_boundary_n50():
    """WR=40%/n=50 — cas limite (ancienne condition etait N>=50)."""
    ub = wilson_upper(0.40, 50)
    # Attendu ~ 0.54 (IC encore large)
    assert ub > 0.50, f"wilson_upper(0.40,50)={ub:.3f}"
    assert contrarian_significant(0.40, 50) is False


# ── Verification numerique independante ───────────────────────────────────────

def test_wilson_lower_30pct_100_manual():
    """Calcul manuel independant de la librairie."""
    p, n, z = 0.30, 100, 1.96
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lb_manual = max(0.0, centre - margin)
    lb_fn = wilson_lower(0.30, 100)
    assert abs(lb_fn - lb_manual) < 1e-10


def test_wilson_upper_40pct_30_manual():
    """Calcul manuel independant de la librairie."""
    p, n, z = 0.40, 30, 1.96
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    ub_manual = min(1.0, centre + margin)
    ub_fn = wilson_upper(0.40, 30)
    assert abs(ub_fn - ub_manual) < 1e-10
