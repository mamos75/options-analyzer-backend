"""
Deribit WebSocket client — récupère options BTC en temps réel.
"""

import asyncio
import json
import random
import time
import websockets
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import logging

DERIBIT_WS = "wss://www.deribit.com/ws/api/v2"
# Max concurrent requests in-flight à tout instant
_CONCURRENCY = 3
# Batching : 15 instruments/batch, 1s entre batches → ≤15 req/s, sous la limite Deribit (20/s)
_FETCH_BATCH_SIZE = 15
_FETCH_BATCH_DELAY = 1.0

# Cache centralisé — UN seul fetch Deribit toutes les 60s minimum
_CACHE_TTL = 45.0        # secondes entre deux fetches — 45s evite la resonance TTL avec les crons 60s
_MAX_BACKOFF = 300.0     # backoff max 5min si over_limit répété

log = logging.getLogger(__name__)


@dataclass
class OptionData:
    instrument: str
    strike: float
    expiry: str
    option_type: str
    oi: float
    volume: float
    gamma: float
    delta: float
    iv: float
    mark_price: float
    bid: float
    ask: float


@dataclass
class MarketSnapshot:
    btc_price: float
    options: List[OptionData] = field(default_factory=list)
    timestamp: float = 0.0


class DeribitClient:
    def __init__(self):
        self._ws = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()
        self._call_sem = asyncio.Semaphore(_CONCURRENCY)
        self._listener_task: Optional[asyncio.Task] = None

        # Cache centralisé — protège Deribit contre le bourrage multi-endpoints
        self._cached_snapshot: Optional[MarketSnapshot] = None
        self._snapshot_ts: float = 0.0
        self._snapshot_lock: Optional[asyncio.Lock] = None  # init lazy (event loop requis)
        self._data_stale: bool = False
        self._backoff_until: float = 0.0
        self._backoff_secs: float = 0.0

    @property
    def data_stale(self) -> bool:
        return self._data_stale

    def _get_snapshot_lock(self) -> asyncio.Lock:
        if self._snapshot_lock is None:
            self._snapshot_lock = asyncio.Lock()
        return self._snapshot_lock

    async def connect(self):
        await self._ensure_connected()

    async def _ensure_connected(self):
        if self._ws and not self._ws.closed:
            return
        async with self._connect_lock:
            # Re-check under the lock
            if self._ws and not self._ws.closed:
                return
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except (asyncio.CancelledError, Exception):
                    pass
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WS reconnecting"))
            self._pending.clear()
            self._ws = await websockets.connect(
                DERIBIT_WS,
                ping_interval=20,
                ping_timeout=30,
                close_timeout=10,
            )
            self._listener_task = asyncio.create_task(self._listener())
            log.info("Deribit WS connecté")

    async def _listener(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending[msg_id]
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"Listener Deribit fermé: {e}")
            # Signal all pending to fail so callers retry
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WS closed"))

    async def _call(self, method: str, params: dict) -> dict:
        await self._ensure_connected()
        async with self._call_sem:
            self._msg_id += 1
            msg_id = self._msg_id
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            self._pending[msg_id] = fut
            try:
                await self._ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": method,
                    "params": params,
                }))
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=15)
                return result.get("result", {})
            finally:
                self._pending.pop(msg_id, None)

    async def get_btc_price(self) -> float:
        data = await self._call("public/ticker", {"instrument_name": "BTC-PERPETUAL"})
        return float(data["mark_price"])

    async def get_instruments(self, expiry: Optional[str] = None) -> List[str]:
        params = {"currency": "BTC", "kind": "option", "expired": False}
        instruments = await self._call("public/get_instruments", params)
        if expiry:
            instruments = [i for i in instruments if expiry in i["instrument_name"]]
        return [i["instrument_name"] for i in instruments]

    async def get_option_data(self, instrument: str) -> Optional[OptionData]:
        try:
            data = await self._call("public/ticker", {"instrument_name": instrument})
            parts = instrument.split("-")
            strike = float(parts[2])
            expiry = parts[1]
            opt_type = "call" if parts[3] == "C" else "put"
            greeks = data.get("greeks", {})
            return OptionData(
                instrument=instrument,
                strike=strike,
                expiry=expiry,
                option_type=opt_type,
                oi=float(data.get("open_interest", 0)),
                volume=float(data.get("stats", {}).get("volume", 0)),
                gamma=float(greeks.get("gamma", 0)),
                delta=float(greeks.get("delta", 0)),
                iv=float(data.get("mark_iv", 0)),
                mark_price=float(data.get("mark_price", 0)),
                bid=float(data.get("best_bid_price", 0)),
                ask=float(data.get("best_ask_price", 0)),
            )
        except Exception as e:
            # Ne pas avaler over_limit — laisser get_full_snapshot le détecter
            if "over_limit" in str(e).lower():
                raise
            log.warning(f"Erreur {instrument}: {e}")
            return None

    async def get_full_snapshot(self, max_instruments: int = 300) -> MarketSnapshot:
        btc_price = await self.get_btc_price()
        instruments = await self.get_instruments()

        # Group by expiry, keep the N most ATM per expiry so ALL expiries are loaded.
        # This ensures vol_structure and vol_smile have data for every term.
        from collections import defaultdict
        by_expiry: dict = defaultdict(list)
        for inst in instruments:
            try:
                parts = inst.split("-")
                strike = float(parts[2])
                expiry = parts[1]
                dist = abs(strike - btc_price) / btc_price
                if dist <= 0.35:
                    by_expiry[expiry].append((dist, inst))
            except Exception:
                continue

        n_expiries = max(len(by_expiry), 1)
        max_per_expiry = max(20, max_instruments // n_expiries)
        relevant = []
        for items in by_expiry.values():
            items.sort(key=lambda x: x[0])
            relevant.extend(inst for _, inst in items[:max_per_expiry])

        relevant = relevant[:max_instruments]

        # Fetch par batches pour rester sous la limite Deribit (20 req/s)
        results = []
        for i in range(0, len(relevant), _FETCH_BATCH_SIZE):
            batch = relevant[i: i + _FETCH_BATCH_SIZE]
            batch_results = await asyncio.gather(
                *[self.get_option_data(inst) for inst in batch],
                return_exceptions=True,
            )
            results.extend(batch_results)
            if i + _FETCH_BATCH_SIZE < len(relevant):
                await asyncio.sleep(_FETCH_BATCH_DELAY)

        over_limit_errors = [
            r for r in results
            if isinstance(r, Exception) and "over_limit" in str(r).lower()
        ]
        if over_limit_errors:
            log.warning(f"[snapshot] {len(over_limit_errors)}/{len(relevant)} over_limit")
        if len(over_limit_errors) > max(10, len(relevant) * 0.15):
            raise RuntimeError(
                f"over_limit: {len(over_limit_errors)}/{len(relevant)} instruments "
                "rate-limited — snapshot incomplet"
            )

        options = [r for r in results if isinstance(r, OptionData)]
        return MarketSnapshot(btc_price=btc_price, options=options, timestamp=time.time())

    async def get_cached_snapshot(self) -> MarketSnapshot:
        """Point d'entrée unique pour tous les endpoints et workers.
        Garantit max 1 fetch Deribit par _CACHE_TTL secondes.
        Backoff exponentiel + snapshot stale si over_limit."""
        now = time.time()

        # Fast path : cache frais, pas de lock
        if self._cached_snapshot and (now - self._snapshot_ts) < _CACHE_TTL:
            return self._cached_snapshot

        lock = self._get_snapshot_lock()
        async with lock:
            now = time.time()
            # Re-vérif sous le lock (un autre waiter a peut-être déjà rafraîchi)
            if self._cached_snapshot and (now - self._snapshot_ts) < _CACHE_TTL:
                return self._cached_snapshot

            # Backoff actif → retourner le snapshot stale plutôt que de surcharger
            if now < self._backoff_until:
                if self._cached_snapshot:
                    self._data_stale = True
                    log.warning(
                        f"[cache] over_limit backoff — {self._backoff_until - now:.0f}s restants "
                        f"— snapshot stale (âge={now - self._snapshot_ts:.0f}s)"
                    )
                    return self._cached_snapshot

            try:
                snapshot = await self.get_full_snapshot()
                self._cached_snapshot = snapshot
                self._snapshot_ts = time.time()
                self._data_stale = False
                self._backoff_secs = 0.0
                self._backoff_until = 0.0
                log.info(
                    f"[cache] Snapshot OK — {len(snapshot.options)} options "
                    f"BTC={snapshot.btc_price:.0f}"
                )
                return snapshot
            except Exception as e:
                err = str(e).lower()
                if "over_limit" in err or "429" in err or "too many" in err or "rate" in err:
                    jitter = random.uniform(5, 20)
                    self._backoff_secs = min(
                        _MAX_BACKOFF,
                        max(60.0, self._backoff_secs * 2) + jitter,
                    )
                    self._backoff_until = time.time() + self._backoff_secs
                    log.warning(
                        f"[cache] over_limit détecté → backoff {self._backoff_secs:.0f}s"
                    )
                else:
                    log.error(f"[cache] Erreur refresh snapshot: {e}")

                if self._cached_snapshot:
                    self._data_stale = True
                    return self._cached_snapshot
                raise

    async def run_background_refresh(self, interval: float = 60.0) -> None:
        """Worker de fond — 1 seul refresh Deribit toutes les `interval` secondes.
        Tous les endpoints lisent le cache ; ce worker est la seule source active."""
        log.info(f"[cache] Background refresh démarré (interval={interval}s)")
        while True:
            try:
                await self.get_cached_snapshot()
            except Exception as e:
                log.error(f"[cache] Background refresh erreur: {e}")
            await asyncio.sleep(interval)

    async def close(self):
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()
