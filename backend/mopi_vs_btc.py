"""
MOPI vs BTC — Validation du Pouvoir Prédictif.

Question unique : "Un trader utilisant le MOPI prend-il de meilleures décisions que sans lui ?"
Juge final : l'historique du marché, pas la théorie.
"""

import sqlite3
import os
import math
import time
from typing import List, Dict, Optional, Tuple

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

RESOLUTIONS_S = {"30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
PERIODS_D     = {"7d": 7, "30d": 30, "90d": 90}
HORIZONS_S    = {"4h": 14400, "24h": 86400, "72h": 259200, "7j": 604800}
COOLDOWN_S    = 14400  # 4h entre deux événements (anti-autocorrélation)


def _load_rows(from_ts: int) -> List[Dict]:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, mopi, gex, dex, btc_price, pc_ratio_near, pc_ratio"
            " FROM metrics_history WHERE ts >= ? ORDER BY ts ASC",
            (from_ts,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _resample(rows: List[Dict], resolution_s: int) -> List[Dict]:
    """Resampling temporel : OHLC pour BTC, moyenne pour MOPI/GEX/DEX/PCR."""
    buckets: Dict[int, List[Dict]] = {}
    for r in rows:
        bt = (r["ts"] // resolution_s) * resolution_s
        buckets.setdefault(bt, []).append(r)

    result = []
    for ts in sorted(buckets.keys()):
        grp = sorted(buckets[ts], key=lambda x: x["ts"])
        mopi_v = [float(r["mopi"]) for r in grp if r.get("mopi") is not None]
        gex_v  = [float(r["gex"])  for r in grp if r.get("gex")  is not None]
        dex_v  = [float(r["dex"])  for r in grp if r.get("dex")  is not None]
        btc_v  = [float(r["btc_price"]) for r in grp
                  if r.get("btc_price") and float(r["btc_price"]) > 0]
        pcr_v  = [float(r.get("pc_ratio_near") or r.get("pc_ratio") or 1.0) for r in grp]
        if not mopi_v or not btc_v:
            continue
        result.append({
            "ts":            ts,
            "mopi":          sum(mopi_v) / len(mopi_v),
            "gex":           sum(gex_v)  / len(gex_v)  if gex_v else 0.0,
            "dex":           sum(dex_v)  / len(dex_v)  if dex_v else 0.0,
            "btc_price":     sum(btc_v)  / len(btc_v),
            "btc_open":      btc_v[0],
            "btc_high":      max(btc_v),
            "btc_low":       min(btc_v),
            "btc_close":     btc_v[-1],
            "pc_ratio_near": sum(pcr_v)  / len(pcr_v),
        })
    return result


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx   = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy   = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return round(num / (dx * dy), 4)


def _correlations(rows: List[Dict]) -> Dict:
    """Corrélation MOPI[t] avec prix BTC et returns futurs à différents horizons."""
    mopi_all: List[float] = []
    price_all: List[float] = []
    r4h_all:  List[Optional[float]] = []
    r24h_all: List[Optional[float]] = []
    r72h_all: List[Optional[float]] = []

    for i, r in enumerate(rows):
        m = r.get("mopi")
        p = r.get("btc_price")
        if m is None or not p or p <= 0:
            continue
        ts             = r["ts"]
        p4h = p24h = p72h = None
        for r2 in rows[i + 1:]:
            dt = r2["ts"] - ts
            if p4h  is None and dt >= 14400:  p4h  = r2.get("btc_price")
            if p24h is None and dt >= 86400:  p24h = r2.get("btc_price")
            if p72h is None and dt >= 259200: p72h = r2.get("btc_price"); break

        mopi_all.append(m)
        price_all.append(p)
        r4h_all.append( (p4h  - p) / p * 100 if p4h  else None)
        r24h_all.append((p24h - p) / p * 100 if p24h else None)
        r72h_all.append((p72h - p) / p * 100 if p72h else None)

    def _paired(returns: List[Optional[float]]) -> Tuple[Optional[List], Optional[List]]:
        pairs = [(m, r) for m, r in zip(mopi_all, returns) if r is not None]
        if len(pairs) < 5:
            return None, None
        xs, ys = zip(*pairs)
        return list(xs), list(ys)

    x4,  y4  = _paired(r4h_all)
    x24, y24 = _paired(r24h_all)
    x72, y72 = _paired(r72h_all)

    return {
        "corr_now":      _pearson(mopi_all, price_all),
        "corr_lead_4h":  _pearson(x4,  y4)  if x4  else None,
        "corr_lead_24h": _pearson(x24, y24) if x24 else None,
        "corr_lead_72h": _pearson(x72, y72) if x72 else None,
        "n_points":      len(mopi_all),
    }


def _sample_quality(n: int) -> str:
    if n < 10:  return "EXPLORATION — échantillon trop faible"
    if n < 30:  return "FRAGILE — validation en cours"
    if n < 100: return "ÉTABLI — signal exploitable"
    return "ROBUSTE — signal confirmé"


def _zone_stats(events: List[Dict], all_rows: List[Dict], direction: str) -> Dict:
    """Perf BTC après chaque événement de zone MOPI.
    direction='above' → long signal ; direction='below' → short signal.
    strategy_return_pct = rendement du trade dans le sens du signal (EV long ou EV short).
    raw_future_return_pct = rendement réel du BTC spot (positif = hausse, négatif = baisse).
    """
    perfs:       Dict[str, List[float]] = {h: [] for h in HORIZONS_S}
    raw_returns: Dict[str, List[float]] = {h: [] for h in HORIZONS_S}

    for ev in events:
        price_now = ev.get("btc_price", 0)
        if not price_now or price_now <= 0:
            continue
        for h_key, h_s in HORIZONS_S.items():
            target = ev["ts"] + h_s
            tol    = max(3600, h_s // 4)
            best_p, best_d = None, float("inf")
            for r in all_rows:
                d = abs(r["ts"] - target)
                if d < best_d and d <= tol:
                    best_d = d
                    best_p = r.get("btc_price")
            if best_p:
                raw = (best_p - price_now) / price_now * 100
                raw_returns[h_key].append(round(raw, 3))
                # strategy_return: positif si le trade gagne (short inversé)
                perf = -raw if direction == "below" else raw
                perfs[h_key].append(round(perf, 3))

    stats: Dict = {}
    for h_key, ps in perfs.items():
        n = len(ps)
        sq = _sample_quality(n)
        if n < 3:
            stats[h_key] = {"n": n, "sample_size": n, "insufficient": True, "sample_quality": sq}
            continue
        winners = [p for p in ps if p > 0]
        losers  = [p for p in ps if p <= 0]
        mean    = sum(ps) / n
        wr      = len(winners) / n * 100
        gm      = sum(winners) / len(winners) if winners else 0.0
        pm      = abs(sum(losers) / len(losers)) if losers else 0.0
        ev_val  = wr / 100 * gm - (1 - wr / 100) * pm
        raws    = raw_returns[h_key]
        # Taux réel BTC spot (indépendant du sens du trade)
        btc_down = round(sum(1 for r in raws if r < 0) / n * 100, 1) if raws else 0.0
        btc_up   = round(sum(1 for r in raws if r > 0) / n * 100, 1) if raws else 0.0
        # Pour signal short (direction="below") : winrate = % BTC a baissé
        # Pour signal long (direction="above") : winrate = % BTC a monté
        btc_down_rate = btc_down
        btc_up_rate   = btc_up
        stats[h_key] = {
            "n":                    n,
            "sample_size":          n,
            "perf_moy":             round(mean, 2),
            "winrate":              round(wr, 1),
            "winrate_strategy_pct": round(wr, 1),
            "gain_moyen":           round(gm, 2),
            "perte_moyenne":        round(pm, 2),
            "ev":                   round(ev_val, 2),
            # Séparation explicite strategy vs spot
            "strategy_return":      round(mean, 2),
            "strategy_return_pct":  round(mean, 2),
            "btc_return_mean":      round(sum(raws) / len(raws), 2) if raws else None,
            "btc_forward_return_pct": round(sum(raws) / len(raws), 2) if raws else None,
            # Fréquences directionnelles BTC spot
            "btc_down_rate_pct":    btc_down_rate,
            "btc_up_rate_pct":      btc_up_rate,
            "sample_quality":       sq,
        }
    return stats


def _extremes(rows: List[Dict], all_rows: List[Dict]) -> Dict:
    """Détecte les entrées en zone MOPI>70 et MOPI<30 avec cooldown 4h."""
    def _detect(threshold: float, direction: str,
                expected_direction: str, signal_label: str) -> Dict:
        events: List[Dict] = []
        last_ts = 0
        for r in rows:
            m = r.get("mopi")
            if m is None:
                continue
            in_zone = (m > threshold) if direction == "above" else (m < threshold)
            if not in_zone:
                last_ts = 0
                continue
            if last_ts > 0 and r["ts"] - last_ts < COOLDOWN_S:
                continue
            last_ts = r["ts"]
            events.append(r)

        trade_side = "long" if direction == "above" else "short"
        condition  = "MOPI > 70" if direction == "above" else "MOPI < 30"
        return {
            "occurrences":        len(events),
            "stats":              _zone_stats(events, all_rows, direction),
            # Sémantique explicite : corrélation positive = MOPI et BTC évoluent ensemble
            "expected_direction": expected_direction,  # "bullish" ou "bearish"
            "signal_label":       signal_label,        # "LONG" ou "SHORT"
            "trade_side":         trade_side,          # "long" ou "short"
            "condition":          condition,            # "MOPI > 70" ou "MOPI < 30"
            "event_timestamps":   [e["ts"] for e in events],
        }

    return {
        "mopi_above_70": _detect(70.0, "above", "bullish", "LONG"),
        "mopi_below_30": _detect(30.0, "below", "bearish", "SHORT"),
    }


def _indicator_pps_score(rows: List[Dict], col: str) -> Dict:
    """Corrélation simple d'un indicateur brut avec BTC return +24h."""
    vals, returns = [], []
    for i, r in enumerate(rows):
        v = r.get(col)
        p = r.get("btc_price")
        if v is None or not p or p <= 0:
            continue
        p24h = None
        for r2 in rows[i + 1:]:
            if r2["ts"] - r["ts"] >= 86400:
                p24h = r2.get("btc_price")
                break
        if p24h is None:
            continue
        vals.append(float(v))
        returns.append((p24h - p) / p * 100)

    c = _pearson(vals, returns)
    abs_c = abs(c) if c is not None else 0.0
    # Calibration : corrélation 0.40 → score 100 (très fort pour données financières)
    score = min(100, round(abs_c * 250))
    return {
        "score":       score,
        "correlation": round(c, 4) if c is not None else None,
        "n_pairs":     len(vals),
    }


def _pps(extremes: Dict, correlations: Dict, rows: List[Dict]) -> Dict:
    """Predictive Power Score MOPI (composite 0-100) + comparaison DEX/GEX."""
    # Corrélation lead 24h (0-30 pts)
    corr_24h = correlations.get("corr_lead_24h")
    abs_c    = abs(corr_24h) if corr_24h is not None else 0
    corr_pts = (30 if abs_c >= 0.6 else 22 if abs_c >= 0.4 else
                14 if abs_c >= 0.2 else  7 if abs_c >= 0.1 else 0)

    # EV moyen à 24h (0-30 pts)
    ev_vals, wr_vals = [], []
    for zone in ["mopi_above_70", "mopi_below_30"]:
        s24 = extremes.get(zone, {}).get("stats", {}).get("24h", {})
        if not s24.get("insufficient") and "ev" in s24:
            ev_vals.append(s24["ev"])
            wr_vals.append(s24["winrate"])

    ev_moy = sum(ev_vals) / len(ev_vals) if ev_vals else 0
    ev_pts = (30 if ev_moy > 3 else 22 if ev_moy > 2 else
              14 if ev_moy > 1 else  7 if ev_moy > 0 else 0)

    # Winrate edge vs 50% (0-20 pts)
    wr_edge = max(0, sum(wr_vals) / len(wr_vals) - 50) if wr_vals else 0
    wr_pts  = (20 if wr_edge >= 20 else 15 if wr_edge >= 15 else
               10 if wr_edge >= 10 else  5 if wr_edge >= 5  else 0)

    # Nombre d'occurrences (0-20 pts)
    total_occ = (
        extremes.get("mopi_above_70", {}).get("occurrences", 0) +
        extremes.get("mopi_below_30", {}).get("occurrences", 0)
    )
    occ_pts = (20 if total_occ >= 50 else 15 if total_occ >= 30 else
               10 if total_occ >= 15 else  5 if total_occ >= 5  else 0)

    mopi_score = min(100, corr_pts + ev_pts + wr_pts + occ_pts)

    # Verdict basé sur le nombre total de signaux (N), pas sur le score composite
    if total_occ < 10:
        verdict = "EXPLORATION — Signal intéressant mais échantillon trop faible"
    elif total_occ < 30:
        verdict = "FRAGILE — Signal prometteur, validation en cours"
    elif total_occ < 100:
        verdict = "ÉTABLI — Signal exploitable, accumulation en cours"
    else:
        verdict = "ROBUSTE — Signal validé sur données suffisantes"

    return {
        "mopi": {
            "score": mopi_score,
            "breakdown": {
                "correlation_24h": corr_pts,
                "expected_value":  ev_pts,
                "winrate_edge":    wr_pts,
                "sample_size":     occ_pts,
            },
        },
        "dex":    _indicator_pps_score(rows, "dex"),
        "gex":    _indicator_pps_score(rows, "gex"),
        "verdict": verdict,
    }


def compute_mopi_vs_btc(period: str = "7d", resolution: str = "1h") -> Dict:
    days  = PERIODS_D.get(period, 7)
    res_s = RESOLUTIONS_S.get(resolution, 3600)

    # +7j de marge pour les returns futurs au bord de la fenêtre
    all_raw = _load_rows(int(time.time()) - (days + 7) * 86400)

    period_cutoff = int(time.time()) - days * 86400
    period_raw    = [r for r in all_raw if r["ts"] >= period_cutoff]

    if not period_raw:
        return {
            "status":   "NO_DATA",
            "message":  "Aucune donnée disponible — accumulation en cours.",
            "timestamps": [], "mopi": [], "btc_ohlc": [], "btc_price": [],
            "gex": [], "dex": [], "pc_ratio_near": [],
            "correlations": {},
            "extremes": {},
            "predictive_power": {
                "mopi":    {"score": 0},
                "dex":     {"score": 0},
                "gex":     {"score": 0},
                "verdict": "Données insuffisantes",
            },
            "crossovers_above_70": [],
            "crossovers_below_30": [],
        }

    period_resampled = _resample(period_raw, res_s)
    all_resampled    = _resample(all_raw,    res_s)

    corrs    = _correlations(all_resampled)
    exts     = _extremes(all_resampled, all_raw)
    pps_data = _pps(exts, corrs, all_resampled)

    co70 = [ts for ts in exts["mopi_above_70"]["event_timestamps"] if ts >= period_cutoff]
    co30 = [ts for ts in exts["mopi_below_30"]["event_timestamps"] if ts >= period_cutoff]

    return {
        "status":     "OK",
        "period":     period,
        "resolution": resolution,
        "n_points":   len(period_resampled),
        "timestamps": [r["ts"]            for r in period_resampled],
        "mopi":       [round(r["mopi"], 1) for r in period_resampled],
        "btc_ohlc":   [
            [r["btc_open"], r["btc_close"], r["btc_low"], r["btc_high"]]
            for r in period_resampled
        ],
        "btc_price":     [round(r["btc_price"], 0) for r in period_resampled],
        "gex":           [round(r["gex"], 0)        for r in period_resampled],
        "dex":           [round(r["dex"], 1)        for r in period_resampled],
        "pc_ratio_near": [round(r["pc_ratio_near"], 3) for r in period_resampled],
        "correlations":        corrs,
        "extremes":            exts,
        "predictive_power":    pps_data,
        "crossovers_above_70": co70,
        "crossovers_below_30": co30,
    }
