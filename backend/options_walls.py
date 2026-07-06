"""
Options Walls — niveaux avec forte concentration d'OI.

Un "mur" d'options agit comme aimant (Max Pain) ou résistance dynamique.
Call wall au-dessus du spot = résistance.
Put wall en-dessous du spot = support.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import statistics

from .deribit_client import MarketSnapshot
from .gex import filter_options_by_dte, DTE_NEAR_MAX, DTE_MONTHLY_MIN, DTE_MONTHLY_MAX
import time as _time
from .options_activity_engine import (
    compute_flow_ratio,
    compute_proximity_score,
    compute_dte_urgency,
    classify_activity_profile,
    TAG_DORMANT,
    ActivityTag,
)


@dataclass
class OptionsWall:
    strike: float
    total_oi: float
    call_oi: float
    put_oi: float
    notional_usd: float
    wall_type: str              # "CALL_WALL" | "PUT_WALL" | "DOUBLE_WALL"
    side: str                   # "RESISTANCE" | "SUPPORT" | "AT_MONEY"
    # Profil d'activité (moteur central)
    structural_score: float = 0.0     # = total_oi (stock brut)
    active_score: float = 0.0         # Σ(oi × flow_ratio) à ce strike
    actionable_score: float = 0.0     # Σ(oi × flow × proximity × dte_urgency)
    volume_24h: float = 0.0           # volume total toutes expiries à ce strike
    dte_nearest_active: int = -1      # DTE de l'expiry la plus active (par contrib. OI×flow)
    tag: ActivityTag = TAG_DORMANT    # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    oi_delta_24h: float = 0.0         # variation OI en BTC sur 24h (négatif = dissolution)


@dataclass
class OptionsWallsProfile:
    walls: List[OptionsWall]          # top 10 triés par OI décroissant
    major_call_wall: Optional[float]  # strike max call OI au-dessus spot (None si aucun call wall)
    major_put_wall: Optional[float]   # strike max put OI en-dessous spot (None si aucun put wall)
    oi_by_strike: Dict[float, dict]   # pour heatmap
    btc_price: float


def compute_options_walls(snapshot: MarketSnapshot) -> OptionsWallsProfile:
    from .gex import _compute_dte

    spot = snapshot.btc_price
    call_oi: Dict[float, float] = {}
    put_oi: Dict[float, float] = {}

    # Données d'activité par strike (calcul en un seul passage)
    volume_by_strike: Dict[float, float] = {}
    active_by_strike: Dict[float, float] = {}      # Σ(oi × flow_ratio)
    actionable_by_strike: Dict[float, float] = {}  # Σ(oi × flow × prox × dte_urg)
    # (best_active_contrib, dte) pour dte_nearest_active
    dte_active_by_strike: Dict[float, Tuple[float, int]] = {}

    for opt in snapshot.options:
        s = opt.strike
        if opt.option_type == "call":
            call_oi[s] = call_oi.get(s, 0) + opt.oi
        else:
            put_oi[s] = put_oi.get(s, 0) + opt.oi

        volume_by_strike[s] = volume_by_strike.get(s, 0) + opt.volume

        flow, _ = compute_flow_ratio(opt)
        active_contrib = opt.oi * flow
        active_by_strike[s] = active_by_strike.get(s, 0) + active_contrib

        dte = _compute_dte(opt.expiry)
        prox = compute_proximity_score(s, spot)
        urgency = compute_dte_urgency(dte)
        actionable_by_strike[s] = (
            actionable_by_strike.get(s, 0) + active_contrib * prox * urgency
        )

        # Expiry la plus active → dte_nearest_active
        prev_best, _ = dte_active_by_strike.get(s, (0.0, -1))
        if active_contrib > prev_best:
            dte_active_by_strike[s] = (active_contrib, dte)

    all_strikes = sorted(set(list(call_oi) + list(put_oi)))
    oi_by_strike: Dict[float, dict] = {}
    for strike in all_strikes:
        c = call_oi.get(strike, 0)
        p = put_oi.get(strike, 0)
        oi_by_strike[strike] = {
            "call_oi": round(c, 2),
            "put_oi": round(p, 2),
            "total_oi": round(c + p, 2),
            "notional_usd": round((c + p) * strike, 0),
        }

    totals = [v["total_oi"] for v in oi_by_strike.values()]
    if not totals:
        return OptionsWallsProfile(
            walls=[], major_call_wall=None, major_put_wall=None,
            oi_by_strike={}, btc_price=spot,
        )

    threshold = statistics.mean(totals) + statistics.stdev(totals) * 1.0

    walls: List[OptionsWall] = []
    for strike, data in oi_by_strike.items():
        if data["total_oi"] < threshold:
            continue
        c, p = data["call_oi"], data["put_oi"]
        wall_type = "DOUBLE_WALL"
        if c > p * 1.8:
            wall_type = "CALL_WALL"
        elif p > c * 1.8:
            wall_type = "PUT_WALL"

        side = "AT_MONEY"
        if strike > spot * 1.005:
            side = "RESISTANCE"
        elif strike < spot * 0.995:
            side = "SUPPORT"

        # Profil d'activité par wall
        total_oi_s = data["total_oi"]
        active_s = active_by_strike.get(strike, 0.0)
        actionable_s = actionable_by_strike.get(strike, 0.0)
        active_pct = (active_s / total_oi_s * 100) if total_oi_s > 1e-9 else 0.0
        actionable_pct = (actionable_s / active_s * 100) if active_s > 1e-9 else 0.0
        tag = classify_activity_profile(active_pct, actionable_pct)
        _, dte_active = dte_active_by_strike.get(strike, (0.0, -1))

        walls.append(OptionsWall(
            strike=strike,
            total_oi=total_oi_s,
            call_oi=c,
            put_oi=p,
            notional_usd=data["notional_usd"],
            wall_type=wall_type,
            side=side,
            structural_score=round(total_oi_s, 2),
            active_score=round(active_s, 4),
            actionable_score=round(actionable_s, 4),
            volume_24h=round(volume_by_strike.get(strike, 0.0), 2),
            dte_nearest_active=dte_active,
            tag=tag,
        ))

    walls.sort(key=lambda w: w.total_oi, reverse=True)

    # F11.3 — OI delta 24h : comparer avec snapshot précédent
    try:
        from . import history_store as _hs
        _prev_oi = _hs.get_walls_oi_prev()
        if _prev_oi:
            for w in walls:
                prev = _prev_oi.get(w.strike)
                if prev is not None and prev > 0:
                    w.oi_delta_24h = round(w.total_oi - prev, 2)
        # Sauvegarder le snapshot courant
        _hs.save_walls_oi_snapshot({w.strike: w.total_oi for w in walls})
    except Exception:
        pass

    # Call wall = strike au-dessus spot avec le + de call OI
    above_calls = {s: v for s, v in call_oi.items() if s > spot}
    major_call_wall = max(above_calls, key=above_calls.get) if above_calls else None
    below_puts = {s: v for s, v in put_oi.items() if s < spot}
    major_put_wall = max(below_puts, key=below_puts.get) if below_puts else None

    return OptionsWallsProfile(
        walls=walls[:20],
        major_call_wall=major_call_wall,
        major_put_wall=major_put_wall,
        oi_by_strike=oi_by_strike,
        btc_price=spot,
    )


def _walls_snapshot(snapshot: MarketSnapshot, dte_min: int, dte_max) -> MarketSnapshot:
    filtered = filter_options_by_dte(snapshot.options, dte_min=dte_min, dte_max=dte_max)
    return MarketSnapshot(btc_price=snapshot.btc_price, options=filtered, timestamp=snapshot.timestamp)


def compute_options_walls_horizons(snapshot: MarketSnapshot) -> dict:
    """
    Retourne 3 OptionsWallsProfiles DTE-aware :
      near    → 0-14j  : walls qui pilotent le marché cette semaine
      monthly → 15-45j : walls swing
      global  → tout   : structure complète
    """
    return {
        "near": compute_options_walls(_walls_snapshot(snapshot, 0, DTE_NEAR_MAX)),
        "monthly": compute_options_walls(_walls_snapshot(snapshot, DTE_MONTHLY_MIN, DTE_MONTHLY_MAX)),
        "global": compute_options_walls(snapshot),
    }
