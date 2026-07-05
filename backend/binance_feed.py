"""
Worker asyncio — collecte funding rate, futures OI et volume spot depuis Binance public API.
Pas de clé API requise. Mise à jour toutes les BINANCE_FEED_INTERVAL secondes (défaut 300s).

Données produites :
  funding_rate       : float | None  — % (ex : 0.0042 = +0.0042%)
  futures_oi         : float | None  — USD notional (ex : 12_500_000_000)
  futures_oi_prev    : float | None  — snapshot précédent (pour delta)
  spot_volume_24h    : float | None  — USD 24h rolling (Binance spot BTCUSDT)
  spot_volume_7d_avg : float | None  — moyenne 7j (pour normalisation)
  last_updated       : float | None  — timestamp unix
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

BINANCE_FEED_INTERVAL = int(os.environ.get("BINANCE_FEED_INTERVAL", 300))

_FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_FUTURES_OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
_SPOT_TICKER_URL     = "https://api.binance.com/api/v3/ticker/24hr"
_SPOT_KLINES_URL     = "https://api.binance.com/api/v3/klines"

_TIMEOUT = aiohttp.ClientTimeout(total=10)

_cache: dict = {
    "funding_rate":       None,
    "futures_oi":         None,
    "futures_oi_prev":    None,
    "spot_volume_24h":    None,
    "spot_volume_7d_avg": None,
    "last_updated":       None,
}

# Ring buffer des derniers OI collectés (pour calculer le delta)
_oi_history: list = []   # liste de (ts, oi_usd)
_OI_HISTORY_MAX = 10     # garder ~50min d'historique à 5min/point


def get_cache() -> dict:
    """Retourne une copie snapshot du cache courant."""
    return dict(_cache)


def is_stale(max_age_seconds: int = 900) -> bool:
    """True si le cache a plus de max_age_seconds secondes d'âge (ou jamais chargé)."""
    lu = _cache.get("last_updated")
    if lu is None:
        return True
    return (time.time() - lu) > max_age_seconds


async def _fetch_funding_rate(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            _FUTURES_FUNDING_URL,
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if data and isinstance(data, list):
                return float(data[-1]["fundingRate"])
    except Exception as e:
        log.warning(f"[binance_feed] funding_rate error: {e}")
    return None


async def _fetch_futures_oi(session: aiohttp.ClientSession) -> Optional[float]:
    """Retourne l'OI open interest en USD notional."""
    try:
        async with session.get(
            _FUTURES_OI_URL,
            params={"symbol": "BTCUSDT"},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            # openInterest est en BTC, on veut USD → on multiplie par le prix moyen
            # Mais l'endpoint /fapi/v1/openInterest retourne uniquement openInterest (BTC)
            # Pour avoir USD on utilise /fapi/v1/ticker/price séparément — mais c'est déjà en cache
            # On garde le BTC et on convertit ici avec le mark price depuis summaryStats
            oi_btc = float(data.get("openInterest", 0))
            # Récupère le mark price via le même endpoint stats (sumOpenInterest = BTC)
            # On stocke directement en BTC, la conversion se fait dans le moteur
            return oi_btc
    except Exception as e:
        log.warning(f"[binance_feed] futures_oi error: {e}")
    return None


async def _fetch_futures_oi_usd(session: aiohttp.ClientSession) -> Optional[float]:
    """OI en USD via fapi/v1/ticker/bookTicker ou sumOpenInterest×markPrice."""
    try:
        # markPrice pour conversion BTC→USD
        async with session.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            mark = await r.json()
            mark_price = float(mark.get("markPrice", 0))

        async with session.get(
            _FUTURES_OI_URL,
            params={"symbol": "BTCUSDT"},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            oi_btc = float(data.get("openInterest", 0))
            return oi_btc * mark_price if mark_price > 0 else None
    except Exception as e:
        log.warning(f"[binance_feed] futures_oi_usd error: {e}")
    return None


async def _fetch_spot_volume_24h(session: aiohttp.ClientSession) -> Optional[float]:
    """Volume spot 24h en USD (quoteVolume)."""
    try:
        async with session.get(
            _SPOT_TICKER_URL,
            params={"symbol": "BTCUSDT"},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return float(data.get("quoteVolume", 0))
    except Exception as e:
        log.warning(f"[binance_feed] spot_volume error: {e}")
    return None


async def _fetch_spot_volume_7d_avg(session: aiohttp.ClientSession) -> Optional[float]:
    """Moyenne du volume spot quotidien sur 7j (klines 1d × 7)."""
    try:
        async with session.get(
            _SPOT_KLINES_URL,
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 7},
            timeout=_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            volumes = [float(k[7]) for k in data]  # k[7] = quoteAssetVolume
            return sum(volumes) / len(volumes) if volumes else None
    except Exception as e:
        log.warning(f"[binance_feed] spot_volume_7d error: {e}")
    return None


async def refresh() -> None:
    """Un cycle de collecte : funding + OI + volume spot."""
    global _cache, _oi_history
    async with aiohttp.ClientSession() as session:
        funding, oi_usd, vol_24h, vol_7d = await asyncio.gather(
            _fetch_funding_rate(session),
            _fetch_futures_oi_usd(session),
            _fetch_spot_volume_24h(session),
            _fetch_spot_volume_7d_avg(session),
            return_exceptions=True,
        )

    # Erreurs → None
    funding  = funding  if isinstance(funding,  float) else None
    oi_usd   = oi_usd   if isinstance(oi_usd,   float) else None
    vol_24h  = vol_24h  if isinstance(vol_24h,  float) else None
    vol_7d   = vol_7d   if isinstance(vol_7d,   float) else None

    # Delta OI
    oi_prev: Optional[float] = None
    if oi_usd is not None:
        _oi_history.append((time.time(), oi_usd))
        if len(_oi_history) > _OI_HISTORY_MAX:
            _oi_history.pop(0)
        if len(_oi_history) >= 2:
            oi_prev = _oi_history[-2][1]

    _cache.update({
        "funding_rate":       funding,
        "futures_oi":         oi_usd,
        "futures_oi_prev":    oi_prev,
        "spot_volume_24h":    vol_24h,
        "spot_volume_7d_avg": vol_7d,
        "last_updated":       time.time(),
    })

    log.info(
        f"[binance_feed] funding={funding} OI={oi_usd and oi_usd/1e9:.2f}B$ "
        f"vol24h={vol_24h and vol_24h/1e9:.2f}B$ vol7d_avg={vol_7d and vol_7d/1e9:.2f}B$"
    )


async def run_worker() -> None:
    """Loop asyncio — collecte Binance en continu."""
    await asyncio.sleep(15)   # laisse le WS Deribit s'établir
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[binance_feed] worker error: {e}")
        await asyncio.sleep(BINANCE_FEED_INTERVAL)
