"""
test_derived_cache.py — Tests Phase B4 :
  - Deux appels meme snapshot = meme objet gex (cache LRU-1)
  - Nouveau snapshot = recalcul
  - invalidate() force un recalcul
"""
from __future__ import annotations
import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch


def _make_snapshot(ts: float, btc_price: float = 50000.0):
    snap = MagicMock()
    snap.timestamp = ts
    snap.btc_price = btc_price
    snap.options = []
    return snap


@pytest.fixture(autouse=True)
def reset_cache():
    """Reinitialise le cache avant et apres chaque test."""
    import backend.derived_cache as dc
    dc.invalidate()
    dc._cache_lock = None  # reset du lock pour les tests synchrones
    yield
    dc.invalidate()
    dc._cache_lock = None


def _run_async(coro):
    """Execute une coroutine dans une nouvelle boucle asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestDerivedCache:

    def test_same_snapshot_returns_same_gex_object(self):
        """
        Deux appels avec le meme snapshot.timestamp retournent le meme objet gex
        (cache LRU-1 — pas de recalcul).
        """
        import backend.derived_cache as dc

        snap = _make_snapshot(ts=1700000000.0)
        call_count = 0
        gex_obj = MagicMock()
        gex_obj.gex_near = 1.0

        def _fake_gex(snapshot):
            nonlocal call_count
            call_count += 1
            return gex_obj

        with patch.object(dc, "compute_gex", _fake_gex), \
             patch.object(dc, "compute_mopi", return_value=MagicMock()), \
             patch.object(dc, "compute_dealer_pressure", return_value=MagicMock()):

            async def _test():
                r1 = await dc.get_derived(snap)
                r2 = await dc.get_derived(snap)
                return r1, r2

            r1, r2 = _run_async(_test())

        assert r1.gex is r2.gex, "Meme snapshot -> meme objet gex (cache)"
        assert call_count == 1, f"compute_gex appele {call_count} fois (attendu 1)"

    def test_different_snapshot_triggers_recompute(self):
        """Nouveau timestamp = recalcul."""
        import backend.derived_cache as dc

        snap1 = _make_snapshot(ts=1700000000.0)
        snap2 = _make_snapshot(ts=1700003600.0)  # 1h plus tard
        call_count = 0

        def _fake_gex(snapshot):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            m.gex_near = float(call_count)
            return m

        with patch.object(dc, "compute_gex", _fake_gex), \
             patch.object(dc, "compute_mopi", return_value=MagicMock()), \
             patch.object(dc, "compute_dealer_pressure", return_value=MagicMock()):

            async def _test():
                r1 = await dc.get_derived(snap1)
                r2 = await dc.get_derived(snap2)
                return r1, r2

            r1, r2 = _run_async(_test())

        assert r1.gex is not r2.gex, "Snapshots differents -> objets differents"
        assert call_count == 2, f"compute_gex appele {call_count} fois (attendu 2)"

    def test_snapshot_ts_is_stored(self):
        """snapshot_ts est correctement stocke dans DerivedMetrics."""
        import backend.derived_cache as dc

        snap = _make_snapshot(ts=9999999.0)
        with patch.object(dc, "compute_gex", return_value=MagicMock()), \
             patch.object(dc, "compute_mopi", return_value=MagicMock()), \
             patch.object(dc, "compute_dealer_pressure", return_value=MagicMock()):

            r = _run_async(dc.get_derived(snap))

        assert r.snapshot_ts == 9999999.0

    def test_invalidate_forces_recompute(self):
        """invalidate() force un recalcul au prochain appel."""
        import backend.derived_cache as dc

        snap = _make_snapshot(ts=1700000000.0)
        call_count = 0

        def _fake_gex(snapshot):
            nonlocal call_count
            call_count += 1
            return MagicMock()

        with patch.object(dc, "compute_gex", _fake_gex), \
             patch.object(dc, "compute_mopi", return_value=MagicMock()), \
             patch.object(dc, "compute_dealer_pressure", return_value=MagicMock()):

            _run_async(dc.get_derived(snap))
            assert call_count == 1

            dc.invalidate()
            dc._cache_lock = None  # reset lock pour la nouvelle boucle

            _run_async(dc.get_derived(snap))
            assert call_count == 2, f"Apres invalidate, recalcul attendu. Appels: {call_count}"
