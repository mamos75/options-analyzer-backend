"""
Génération de données de démonstration pour le backtest.

Utilise les vrais prix BTC historiques (Binance API) + métriques options synthétiques
corrélées au momentum BTC. Permet d'afficher le format du rapport avant accumulation
des données réelles Deribit.

Clairement labellisé : SIMULATION — métriques options sont des proxies, pas les vraies
données Deribit. À remplacer par les vraies stats dans ~30 jours.
"""

import random
import math
import time
from typing import List, Dict, Optional


def _fetch_btc_history_binance(days: int = 90) -> List[Dict]:
    """Télécharge les prix BTC horaires depuis Binance (API gratuite)."""
    import urllib.request
    import json

    limit = min(days * 24, 1000)
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval=1h&limit={limit}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        result = []
        for candle in data:
            result.append({
                "ts": int(candle[0]) // 1000,
                "btc_price": float(candle[4]),  # close
                "high": float(candle[2]),
                "low": float(candle[3]),
                "volume": float(candle[5]),
            })
        return result
    except Exception as e:
        return []


def _compute_hv(prices: List[float], window: int = 24 * 7) -> List[float]:
    """Volatilité historique annualisée sur une fenêtre glissante (proxy IV)."""
    hvs = []
    for i in range(len(prices)):
        start = max(0, i - window + 1)
        window_prices = prices[start:i + 1]
        if len(window_prices) < 2:
            hvs.append(0.6)
            continue
        returns = [
            math.log(window_prices[j] / window_prices[j - 1])
            for j in range(1, len(window_prices))
        ]
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_hourly = math.sqrt(variance)
        hv_annual = std_hourly * math.sqrt(24 * 365)
        hvs.append(hv_annual)
    return hvs


def _iv_rank(hvs: List[float], i: int, lookback: int = 90 * 24) -> float:
    """Rank de la HV courante dans son historique lookback (0-100)."""
    start = max(0, i - lookback + 1)
    window = hvs[start:i + 1]
    if len(window) < 2:
        return 50.0
    low, high = min(window), max(window)
    if high == low:
        return 50.0
    return (hvs[i] - low) / (high - low) * 100


def _momentum(prices: List[float], i: int, window_h: int) -> float:
    """Retour en % sur les dernières window_h heures."""
    j = max(0, i - window_h)
    if prices[j] <= 0:
        return 0.0
    return (prices[i] - prices[j]) / prices[j] * 100


def generate_synthetic_snapshots(days: int = 90) -> List[Dict]:
    """Génère des snapshots synthétiques corrélés aux vrais prix BTC."""
    rng = random.Random(42)

    candles = _fetch_btc_history_binance(days)
    if not candles:
        return []

    prices = [c["btc_price"] for c in candles]
    hvs = _compute_hv(prices, window=7 * 24)

    # Garder 1 snapshot toutes les 2h pour simuler HISTORY_INTERVAL=7200s
    step = 2
    snapshots = []

    for i in range(0, len(candles), step):
        c = candles[i]
        price = c["btc_price"]
        ts = c["ts"]

        # ── IV Rank proxy ───────────────────────────────────────────────
        ivr = _iv_rank(hvs, i)
        iv_rank = round(max(0, min(100, ivr + rng.gauss(0, 8))), 1)

        # ── Momentum ────────────────────────────────────────────────────
        mom_1h  = _momentum(prices, i, 1)
        mom_24h = _momentum(prices, i, 24)
        mom_72h = _momentum(prices, i, 72)
        mom_7d  = _momentum(prices, i, 7 * 24)

        # ── GEX proxy ────────────────────────────────────────────────────
        # Marché calme (HV basse) → GEX positif (dealers stabilisants)
        # Marché volatile (HV haute) → GEX négatif (dealers amplificateurs)
        hv_current = hvs[i]
        # Seuil ~0.80 annualisé = HV haute pour BTC
        gex_base = (0.85 - hv_current) * 3_000_000_000  # ~±3B
        gex_noise = rng.gauss(0, 500_000_000)
        gex = round(gex_base + gex_noise, 0)

        # ── MOPI proxy ──────────────────────────────────────────────────
        # Corrélé au momentum 72h + IV rank inversé
        mopi_signal = 50 + 2.5 * mom_72h + (50 - iv_rank) * 0.3
        mopi = round(max(5, min(95, mopi_signal + rng.gauss(0, 12))), 1)

        # ── DEX proxy ───────────────────────────────────────────────────
        # Si BTC monte → traders achètent calls → dealers short calls → delta négatif
        dex_base = -mom_24h * 300  # momentum 24h → DEX
        dex = round(dex_base + rng.gauss(0, 1500), 1)

        # ── PCR proxy (contrarian) ───────────────────────────────────────
        # IV élevée → marché craintif → plus de puts → pcr monte
        pc_ratio_global = round(max(0.4, min(2.5, 1.0 + iv_rank / 200 + rng.gauss(0, 0.15))), 3)
        # Near-term : plus sensible au momentum court terme
        pcr_near_signal = pc_ratio_global + rng.gauss(0, 0.2)
        pc_ratio_near = round(max(0.3, min(2.8, pcr_near_signal)), 3)

        # ── Max Pain proxy ────────────────────────────────────────────────
        # Attireur vers la moyenne pondérée des 14 derniers jours
        start_j = max(0, i - 14 * 24)
        avg_14d = sum(prices[start_j:i + 1]) / max(1, i - start_j + 1)
        max_pain_raw = avg_14d * (1 + rng.gauss(0, 0.02))
        max_pain = round(max_pain_raw / 500) * 500  # arrondi au 500$

        # ── Flip Level proxy ─────────────────────────────────────────────
        flip_ratio = 0.97 + rng.gauss(0, 0.015)
        flip_level = round(max_pain * flip_ratio / 500) * 500

        snapshots.append({
            "ts": ts,
            "btc_price": price,
            "mopi": mopi,
            "gex": gex,
            "dex": dex,
            "iv_rank": iv_rank,
            "pc_ratio": pc_ratio_global,
            "pc_ratio_near": pc_ratio_near,
            "max_pain": max_pain,
            "flip_level": flip_level,
        })

    return snapshots


def run_demo_backtest(days: int = 90) -> Dict:
    """Génère un backtest de démonstration sur données synthétiques BTC réels + options proxies."""
    from .backtest import BacktestEngine, compute_signal_mamos_v1, WEIGHTS_V1

    snapshots = generate_synthetic_snapshots(days)
    if not snapshots:
        return {
            "status": "ERROR",
            "message": "Impossible de télécharger les données BTC depuis Binance.",
        }

    n = len(snapshots)
    ts_min = snapshots[0]["ts"]
    ts_max = snapshots[-1]["ts"]
    span_days = (ts_max - ts_min) / 86400

    engine = BacktestEngine()
    enriched = engine.enrich_with_outcomes(snapshots)

    has_24h = any(r.get("perf_24h") is not None for r in enriched)

    if not has_24h:
        return {
            "status": "DEMO_ERROR",
            "message": f"Données téléchargées ({n} pts) mais horizons non calculables.",
        }

    results = {
        "mopi":      engine.backtest_mopi(enriched),
        "gex":       engine.backtest_gex(enriched),
        "dex":       engine.backtest_dex(enriched),
        "pcr":       engine.backtest_pcr(enriched),
        "max_pain":  engine.backtest_max_pain(enriched),
        "squeeze":   engine.backtest_squeeze_proxy(enriched),
        "gravity":   engine.backtest_gravity(enriched),
    }
    signal_v1 = engine.backtest_signal_v1(enriched)
    confluence = engine.backtest_confluence(enriched)
    weights_rec = engine.recommend_weights(results)

    import datetime
    full_result = {
        "status": "DEMO",
        "disclaimer": (
            "SIMULATION PRÉLIMINAIRE — Prix BTC réels (Binance) + métriques options synthétiques. "
            "Remplacé par les vraies données Deribit dans ~30 jours."
        ),
        "meta": {
            "n_snapshots": n,
            "span_days": round(span_days, 1),
            "from": datetime.datetime.utcfromtimestamp(ts_min).strftime("%Y-%m-%d %H:%M UTC"),
            "to": datetime.datetime.utcfromtimestamp(ts_max).strftime("%Y-%m-%d %H:%M UTC"),
            "data_source": "btc_price=Binance réel | options=proxies synthétiques",
            "horizons_ready": {
                "4h":  any(r.get("perf_4h")  is not None for r in enriched),
                "24h": any(r.get("perf_24h") is not None for r in enriched),
                "72h": any(r.get("perf_72h") is not None for r in enriched),
                "7j":  any(r.get("perf_7j")  is not None for r in enriched),
            },
        },
        "components": results,
        "signal_v1": signal_v1,
        "confluence": confluence,
        "weights": weights_rec,
    }
    full_result["text_report"] = engine.generate_text_report(
        results, signal_v1, weights_rec, confluence, n, span_days
    )
    full_result["vip_argument"] = engine.generate_vip_argument(full_result)
    return full_result
