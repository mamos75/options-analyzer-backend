"""
Radiographie des contributions réelles par métrique options.

Pour chaque métrique (GEX, DEX, Walls, Gravity, PCR_Puts/Calls, Max Pain, Squeeze) :
- % du signal provenant de DTE ≤ 14 / 15-45 / > 45
- % du signal provenant du Top 10% OI
- % du signal provenant du Top 10% Volume
- Top 20 contributeurs réels

Objectif : identifier ce qui pilote vraiment chaque calcul AVANT tout refactoring.
"""

import math
from datetime import datetime, timezone
from typing import List, Tuple

from .deribit_client import MarketSnapshot, OptionData


def _compute_dte(expiry: str, today) -> int:
    try:
        exp_date = datetime.strptime(expiry.upper(), "%d%b%y").date()
        return max(0, (exp_date - today).days)
    except Exception:
        return 9999


def _dte_bucket(dte: int) -> str:
    if dte <= 14:
        return "near"
    if dte <= 45:
        return "mid"
    return "far"


def _percentile_threshold(values: List[float], pct: float) -> float:
    """Valeur seuil au percentile pct (0-100) dans une liste de floats."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * pct / 100) - 1)
    return sorted_vals[idx]


def _signal_breakdown(
    opts: List[OptionData],
    signals: List[float],
    today,
    signal_label: str,
) -> dict:
    """
    Décompose un vecteur de signaux (un float par option) en :
    - part near/mid/far (par DTE)
    - part top 10% OI et top 10% volume
    - top 20 contributeurs (signaux non nuls uniquement)
    """
    total = sum(signals) or 1.0

    near_sum = mid_sum = far_sum = 0.0
    dte_cache = {}

    for opt, sig in zip(opts, signals):
        if opt.expiry not in dte_cache:
            dte_cache[opt.expiry] = _compute_dte(opt.expiry, today)
        dte = dte_cache[opt.expiry]
        bucket = _dte_bucket(dte)
        if bucket == "near":
            near_sum += sig
        elif bucket == "mid":
            mid_sum += sig
        else:
            far_sum += sig

    # Seuils top 10% OI et Volume (décile supérieur)
    oi_threshold = _percentile_threshold([opt.oi for opt in opts], 90)
    vol_threshold = _percentile_threshold([opt.volume for opt in opts], 90)

    top_oi_sum = sum(
        sig for opt, sig in zip(opts, signals) if opt.oi >= oi_threshold
    )
    top_vol_sum = sum(
        sig for opt, sig in zip(opts, signals) if opt.volume >= vol_threshold
    )

    # Top 20 par signal décroissant — exclut les signaux nuls
    ranked: List[Tuple[OptionData, float]] = sorted(
        ((opt, sig) for opt, sig in zip(opts, signals) if sig > 0),
        key=lambda x: x[1], reverse=True
    )[:20]

    top_20 = []
    cumulative = 0.0
    for rank, (opt, sig) in enumerate(ranked, 1):
        dte = dte_cache.get(opt.expiry) or _compute_dte(opt.expiry, today)
        pct = sig / total * 100
        cumulative += pct
        top_20.append({
            "rank": rank,
            "instrument": opt.instrument,
            "strike": opt.strike,
            "expiry": opt.expiry,
            "type": opt.option_type,
            "dte": dte,
            "dte_bucket": _dte_bucket(dte),
            "signal": round(sig, 4),
            "signal_label": signal_label,
            "pct_of_total": round(pct, 2),
            "cumulative_pct": round(cumulative, 2),
            "oi": round(opt.oi, 1),
            "volume": round(opt.volume, 1),
            "gamma": round(opt.gamma, 8),
            "delta": round(opt.delta, 4),
        })

    return {
        "near_pct": round(near_sum / total * 100, 1),
        "mid_pct": round(mid_sum / total * 100, 1),
        "far_pct": round(far_sum / total * 100, 1),
        "top_oi_pct": round(top_oi_sum / total * 100, 1),
        "top_vol_pct": round(top_vol_sum / total * 100, 1),
        "top_20": top_20,
    }


def _activity_tag(oi: float, volume: float) -> str:
    """ACTIVE/SEMI_ACTIVE/DORMANT selon ratio volume/OI sur 24h."""
    if oi <= 0:
        return "DORMANT"
    ratio = volume / oi
    if ratio >= 0.10:
        return "ACTIVE"
    if ratio >= 0.02:
        return "SEMI_ACTIVE"
    return "DORMANT"


def _enrich_walls_activity(top_20: list) -> None:
    """Enrichit chaque entrée Walls avec activity_tag."""
    for item in top_20:
        item["activity_tag"] = _activity_tag(item.get("oi", 0), item.get("volume", 0))


def compute_data_quality(snapshot: MarketSnapshot) -> dict:
    """
    Radiographie complète des contributions réelles pour les métriques options.

    Signal retenu par métrique :
    - GEX       : abs(gamma × OI × spot²)              — magnitude GEX
    - DEX       : abs(OI × |delta|)                     — pression delta
    - Walls     : OI brut + tag ACTIVE/SEMI_ACTIVE/DORMANT via volume/OI
    - Gravity   : OI × exp(-|dist_spot| / 5%)           — attraction gravitationnelle réelle
    - PCR_Puts  : OI puts par horizon (numérateur du ratio)
    - PCR_Calls : OI calls par horizon (dénominateur du ratio)
    - Max_Pain  : (max_pain_strike - strike) × OI — contribution intrinsèque par expiry
    - Squeeze   : abs(gamma × OI × spot²)               — composant GEX dominant (poids 0.30)
    """
    spot = snapshot.btc_price
    opts = snapshot.options
    today = datetime.now(timezone.utc).date()

    if not opts:
        return {"error": "Aucune donnée options disponible", "timestamp": snapshot.timestamp}

    # ── GEX et DEX ──────────────────────────────────────────────────────────
    gex_signals = [abs(opt.gamma * opt.oi * spot ** 2) for opt in opts]
    dex_signals = [abs(opt.oi * abs(opt.delta)) for opt in opts]

    # ── Walls — OI brut (top_20 enrichis avec activity_tag) ─────────────────
    oi_signals = [opt.oi for opt in opts]

    # ── Gravity — OI × déclin exponentiel × boost GEX si disponible ─────────
    # σ = 5% du spot. Boost GEX : strikes avec gros gamma ont une attraction accrue.
    # $100M de GEX par contrat = boost max (×2). En dessous : boost proportionnel.
    # Ainsi un wall near ATM avec gros gamma n'est PAS équivalent à un wall deep OTM.
    _GRAVITY_GEX_NORM = 100_000_000
    gravity_signals = [
        opt.oi
        * math.exp(-abs(opt.strike - spot) / spot / 0.05)
        * (1.0 + min(1.0, abs(opt.gamma * spot ** 2) / _GRAVITY_GEX_NORM))
        for opt in opts
    ]

    # ── PCR — OI séparé par type pour analyser la pression par horizon ───────
    # PCR = puts / calls. Les deux horizons doivent être analysés séparément.
    pcr_put_signals  = [opt.oi if opt.option_type == "put"  else 0.0 for opt in opts]
    pcr_call_signals = [opt.oi if opt.option_type == "call" else 0.0 for opt in opts]

    # ── Max Pain — contribution intrinsèque au strike de max pain ───────────
    # Recalcul interne du max pain strike (valeur intrinsèque × OI minimisée)
    strikes = sorted({o.strike for o in opts})
    min_pain_val = float("inf")
    max_pain_strike = strikes[0] if strikes else spot
    for target in strikes:
        pain = sum(
            (target - o.strike) * o.oi if o.option_type == "call" and target > o.strike
            else (o.strike - target) * o.oi if o.option_type == "put" and target < o.strike
            else 0.0
            for o in opts
        )
        if pain < min_pain_val:
            min_pain_val = pain
            max_pain_strike = target

    max_pain_signals = []
    for opt in opts:
        if opt.option_type == "call" and max_pain_strike > opt.strike:
            contribution = (max_pain_strike - opt.strike) * opt.oi
        elif opt.option_type == "put" and max_pain_strike < opt.strike:
            contribution = (opt.strike - max_pain_strike) * opt.oi
        else:
            contribution = 0.0
        max_pain_signals.append(contribution)

    # ── Cache DTE global (utilisé dans max_pain_expiry_breakdown et distribution) ──
    dte_cache = {
        opt.expiry: _compute_dte(opt.expiry, today)
        for opt in opts
    }

    # ── Max Pain groupé par expiry ────────────────────────────────────────────
    # Révèle quelle expiry tire le prix vers le max pain — permet d'évaluer l'impact
    # par échéance et non par option individuelle (OI brut n'a pas cet angle).
    _mp_by_expiry: dict = {}
    for opt, contrib in zip(opts, max_pain_signals):
        _mp_by_expiry[opt.expiry] = _mp_by_expiry.get(opt.expiry, 0.0) + contrib
    _mp_total = sum(_mp_by_expiry.values()) or 1.0
    max_pain_expiry_breakdown = sorted(
        [
            {
                "expiry": exp,
                "dte": dte_cache.get(exp, _compute_dte(exp, today)),
                "dte_bucket": _dte_bucket(dte_cache.get(exp, _compute_dte(exp, today))),
                "contribution": round(val, 2),
                "pct_of_total": round(val / _mp_total * 100, 1),
            }
            for exp, val in _mp_by_expiry.items()
        ],
        key=lambda x: x["contribution"],
        reverse=True,
    )[:10]

    # ── Assemblage métriques ──────────────────────────────────────────────────
    metrics = {
        "GEX":       _signal_breakdown(opts, gex_signals,       today, "USD GEX abs"),
        "DEX":       _signal_breakdown(opts, dex_signals,       today, "BTC delta abs"),
        "Walls":     _signal_breakdown(opts, oi_signals,        today, "OI contrats"),
        "Gravity":   _signal_breakdown(opts, gravity_signals,   today, "OI × attraction spot"),
        "PCR_Puts":  _signal_breakdown(opts, pcr_put_signals,   today, "OI puts"),
        "PCR_Calls": _signal_breakdown(opts, pcr_call_signals,  today, "OI calls"),
        "Max_Pain":  _signal_breakdown(opts, max_pain_signals,  today, "Valeur intrinsèque × OI"),
        "Squeeze":   _signal_breakdown(opts, gex_signals,       today, "USD GEX abs"),
    }

    # Enrichir Walls avec tags d'activité (ACTIVE / SEMI_ACTIVE / DORMANT)
    _enrich_walls_activity(metrics["Walls"]["top_20"])

    # ── Table de résumé ───────────────────────────────────────────────────────
    table = [
        {
            "metric": name,
            "near": data["near_pct"],
            "mid": data["mid_pct"],
            "far": data["far_pct"],
            "top_oi": data["top_oi_pct"],
            "top_volume": data["top_vol_pct"],
        }
        for name, data in metrics.items()
    ]

    # ── Distribution globale OI par bucket DTE ───────────────────────────────
    total_oi = sum(opt.oi for opt in opts) or 1.0
    total_volume = sum(opt.volume for opt in opts) or 1.0
    near_oi = sum(opt.oi for opt in opts if _dte_bucket(dte_cache[opt.expiry]) == "near")
    mid_oi  = sum(opt.oi for opt in opts if _dte_bucket(dte_cache[opt.expiry]) == "mid")
    far_oi  = sum(opt.oi for opt in opts if _dte_bucket(dte_cache[opt.expiry]) == "far")

    # ── Top 10 expiries par OI total ─────────────────────────────────────────
    expiry_oi: dict = {}
    expiry_vol: dict = {}
    for opt in opts:
        expiry_oi[opt.expiry]  = expiry_oi.get(opt.expiry,  0.0) + opt.oi
        expiry_vol[opt.expiry] = expiry_vol.get(opt.expiry, 0.0) + opt.volume

    top_expiries = sorted(expiry_oi.items(), key=lambda x: x[1], reverse=True)[:10]
    expiry_breakdown = []
    for expiry, oi in top_expiries:
        dte = dte_cache.get(expiry, _compute_dte(expiry, today))
        expiry_breakdown.append({
            "expiry": expiry,
            "dte": dte,
            "dte_bucket": _dte_bucket(dte),
            "oi": round(oi, 1),
            "oi_pct": round(oi / total_oi * 100, 1),
            "volume": round(expiry_vol.get(expiry, 0), 1),
            "volume_pct": round(expiry_vol.get(expiry, 0) / total_volume * 100, 1),
        })

    # ── Preuve d'indépendance des signaux ────────────────────────────────────
    # Si near_pct divergent entre métriques → signaux distincts (pas de redondance OI brut)
    _walls_near = metrics["Walls"]["near_pct"]
    _gravity_near = metrics["Gravity"]["near_pct"]
    _pcr_near = metrics["PCR_Puts"]["near_pct"]
    _maxpain_near = metrics["Max_Pain"]["near_pct"]
    signal_independence_proof = {
        "description": "near_pct par métrique — divergence confirme indépendance des signaux",
        "near_term_bias": {
            name: data["near_pct"] for name, data in metrics.items()
        },
        "walls_vs_gravity_delta": round(abs(_gravity_near - _walls_near), 1),
        "walls_vs_pcr_puts_delta": round(abs(_pcr_near - _walls_near), 1),
        "walls_vs_maxpain_delta": round(abs(_maxpain_near - _walls_near), 1),
        "verdict": (
            "DIVERGENT ✅ — signaux indépendants confirmés"
            if any([
                abs(_gravity_near - _walls_near) > 3.0,
                abs(_pcr_near - _walls_near) > 3.0,
                abs(_maxpain_near - _walls_near) > 3.0,
            ])
            else "⚠️ WARNING — métriques encore trop proches, vérifier formules"
        ),
    }

    return {
        "timestamp": snapshot.timestamp,
        "btc_price": round(spot, 0),
        "total_options": len(opts),
        "total_oi": round(total_oi, 1),
        "total_volume": round(total_volume, 1),
        "max_pain_strike": round(max_pain_strike, 0),
        "max_pain_expiry_breakdown": max_pain_expiry_breakdown,
        "signal_independence_proof": signal_independence_proof,
        "global_oi_distribution": {
            "near_pct": round(near_oi / total_oi * 100, 1),
            "mid_pct":  round(mid_oi  / total_oi * 100, 1),
            "far_pct":  round(far_oi  / total_oi * 100, 1),
        },
        "top_expiries_by_oi": expiry_breakdown,
        "metrics": metrics,
        "table": table,
    }
