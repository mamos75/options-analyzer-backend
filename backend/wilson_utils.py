"""
wilson_utils.py — Intervalle de Wilson pour proportion binaire.

Partagé entre le backend (gating BME, contrarian) et potentiellement
l'API (pour que le frontend lise has_edge serveur).

Référence : Wilson, E.B. (1927). "Probable inference, the law of succession,
            and statistical inference." JASA 22, 209-212.
"""
from __future__ import annotations
import math


def wilson_bounds(wr: float, n: int, z: float = 1.96) -> tuple:
    """
    Retourne (lower_bound, upper_bound) de l'intervalle de Wilson a z sigma.

    - z=1.96  -> 95 % IC bilateral
    - z=1.645 -> 95 % IC unilateral

    Si n <= 0, retourne (0.0, 1.0) (aucune information).
    """
    if n <= 0:
        return 0.0, 1.0
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (wr + z2 / (2 * n)) / denom
    margin = z * math.sqrt(wr * (1 - wr) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def wilson_lower(wr: float, n: int, z: float = 1.96) -> float:
    """Borne inferieure de Wilson (borne conservative du winrate reel)."""
    return wilson_bounds(wr, n, z)[0]


def wilson_upper(wr: float, n: int, z: float = 1.96) -> float:
    """Borne superieure de Wilson."""
    return wilson_bounds(wr, n, z)[1]


def has_edge(wr, n: int, min_n: int = 30, z: float = 1.96) -> bool:
    """
    Retourne True si le WR est significativement > 50 % selon Wilson.

    Condition : n >= min_n  ET  wilson_lower(wr, n) > 0.50
    """
    if wr is None or n < min_n:
        return False
    return wilson_lower(wr, n, z) > 0.50


def contrarian_significant(wr, n: int, min_n: int = 30, z: float = 1.96) -> bool:
    """
    Retourne True si le WR est significativement < 50 % (justifie inversion).

    Condition : n >= min_n  ET  wilson_upper(wr, n) < 0.50
    La borne HAUTE est < 0.50 = le WR est significativement pire que le hasard.
    """
    if wr is None or n < min_n:
        return False
    return wilson_upper(wr, n, z) < 0.50
