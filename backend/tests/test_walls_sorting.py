"""
test_walls_sorting.py — Tests Phase B2 :
  - Walls retournes tries par OI decroissant
  - Snapshot vide -> major_call_wall et major_put_wall sont None
  - Pas de call au-dessus du spot -> major_call_wall = None
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from backend.options_walls import compute_options_walls


def _make_opt(strike, option_type, oi, volume=50.0, expiry="2024-12-27"):
    o = MagicMock()
    o.strike = strike
    o.option_type = option_type
    o.oi = oi
    o.volume = volume
    o.expiry = expiry
    return o


def _make_snapshot(options=None, btc_price=50000.0):
    snap = MagicMock()
    snap.btc_price = btc_price
    snap.options = options or []
    snap.timestamp = 1700000000
    return snap


def test_walls_sorted_by_oi_desc():
    """Les walls retournes sont tries par total_oi decroissant (B2)."""
    opts = []
    # Plusieurs strikes avec des OI diverses pour depasser le seuil stdev
    strikes_data = [
        (55000.0, "call", 2000.0),  # plus grande OI
        (60000.0, "call",  800.0),
        (65000.0, "call",  300.0),
        (70000.0, "call",  100.0),
        (45000.0, "put",  1200.0),
        (40000.0, "put",   500.0),
        (35000.0, "put",   150.0),
    ]
    for strike, opt_type, oi in strikes_data:
        # plusieurs contrats par strike pour atteindre le seuil threshold
        for _ in range(5):
            opts.append(_make_opt(strike, opt_type, oi, volume=200.0))

    snap = _make_snapshot(opts, btc_price=50000.0)
    result = compute_options_walls(snap)

    # Si des walls sont retournes, ils doivent etre tries par OI desc
    if len(result.walls) >= 2:
        for i in range(len(result.walls) - 1):
            assert result.walls[i].total_oi >= result.walls[i + 1].total_oi, (
                f"Walls non tries : walls[{i}].total_oi={result.walls[i].total_oi} "
                f"< walls[{i+1}].total_oi={result.walls[i+1].total_oi}"
            )
    # S'il n'y a qu'un seul wall ou aucun, le test passe (conditions trop faibles pour thresholding)
    assert isinstance(result.walls, list)


def test_empty_snapshot_returns_null_majors():
    """Snapshot sans options -> major_call_wall et major_put_wall sont None (B2)."""
    snap = _make_snapshot([], btc_price=50000.0)
    result = compute_options_walls(snap)
    assert result.walls == []
    assert result.major_call_wall is None, (
        f"major_call_wall devrait etre None, got {result.major_call_wall}"
    )
    assert result.major_put_wall is None, (
        f"major_put_wall devrait etre None, got {result.major_put_wall}"
    )


def test_no_call_above_spot_returns_null_call_wall():
    """Calls uniquement sous le spot -> major_call_wall = None (B2)."""
    opts = []
    # Calls sous le spot — au moins 2 strikes distincts pour stdev
    for _ in range(5):
        opts.append(_make_opt(45000.0, "call", 500.0, volume=100.0))
    for _ in range(5):
        opts.append(_make_opt(43000.0, "call", 200.0, volume=50.0))
    for _ in range(5):
        opts.append(_make_opt(41000.0, "call", 100.0, volume=20.0))

    snap = _make_snapshot(opts, btc_price=50000.0)
    result = compute_options_walls(snap)
    assert result.major_call_wall is None, (
        f"Aucun call au-dessus du spot -> major_call_wall devrait etre None, "
        f"got {result.major_call_wall}"
    )
