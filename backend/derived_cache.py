"""
derived_cache.py — Cache de metriques derivees par snapshot (Phase B4).

Garantit qu'un seul calcul gex/mopi/dealer_pressure se fait par snapshot.timestamp.
Tous les endpoints consomment le meme objet calcule : coherence garantie.

Cache LRU taille 1 (seul le dernier snapshot compte) + asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .gex import compute_gex
from .mopi import compute_mopi
from .dealer_pressure import compute_dealer_pressure

log = logging.getLogger(__name__)

# ── Cache interne (LRU-1) ────────────────────────────────────────────────────
_cache_ts: float = -1.0
_cache_gex: Any = None
_cache_mopi: Any = None
_cache_dp: Any = None
_cache_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Cree le lock paresseusement (besoin d'une boucle asyncio active)."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


class DerivedMetrics:
    """Container pour les metriques derivees d'un snapshot."""
    __slots__ = ("gex", "mopi", "dp", "snapshot_ts", "computed_at")

    def __init__(self, gex, mopi, dp, snapshot_ts: float):
        self.gex = gex
        self.mopi = mopi
        self.dp = dp
        self.snapshot_ts = snapshot_ts
        self.computed_at = time.time()


async def get_derived(
    snapshot,
    iv_history_90d=None,
    gex_near_cap: float = 500_000_000,
    cap_mode: str = "static/bootstrap",
    saturation_rate_7d=None,
) -> DerivedMetrics:
    """
    Retourne les metriques gex/mopi/dp du snapshot.

    Cache LRU-1 sur snapshot.timestamp — recalcul seulement si nouveau snapshot.
    """
    global _cache_ts, _cache_gex, _cache_mopi, _cache_dp

    lock = _get_lock()
    async with lock:
        snap_ts = float(snapshot.timestamp)
        if _cache_ts == snap_ts and _cache_gex is not None:
            return DerivedMetrics(_cache_gex, _cache_mopi, _cache_dp, snap_ts)

        t0 = time.time()
        gex  = compute_gex(snapshot)
        mopi = compute_mopi(
            snapshot, gex, iv_history_90d or [],
            gex_near_cap=gex_near_cap,
            cap_mode=cap_mode,
            saturation_rate_7d=saturation_rate_7d,
        )
        dp = compute_dealer_pressure(snapshot)

        _cache_ts   = snap_ts
        _cache_gex  = gex
        _cache_mopi = mopi
        _cache_dp   = dp

        elapsed = round(time.time() - t0, 3)
        log.info(f"[derived_cache] ts={snap_ts:.0f} computed in {elapsed}s (gex+mopi+dp)")
        return DerivedMetrics(gex, mopi, dp, snap_ts)


def invalidate() -> None:
    """Force un recalcul au prochain appel (tests, debug)."""
    global _cache_ts
    _cache_ts = -1.0
