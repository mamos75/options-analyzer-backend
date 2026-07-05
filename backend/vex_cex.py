"""
vex_cex.py — Vanna Exposure (VEX) et Charm Exposure (CEX).

VEX = Sigma(vanna x OI x spot)  — sensibilite du flow dealer a un mouvement de vol implicite.
CEX = Sigma(charm x OI)         — decay du flow dealer dans le temps (theta du delta).

Hypothese : dealers short toutes les options (calls et puts) — meme convention que DEX.

Formules Black-Scholes :
  d1   = (ln(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
  d2   = d1 - sigma*sqrt(T)
  N_prime(x) = exp(-x**2/2) / sqrt(2*pi)   [PDF normale standard]
  vanna = -N_prime(d1) * d2 / sigma
  charm = -N_prime(d1) * (2rT - d2*sigma*sqrt(T)) / (2T*sigma*sqrt(T)) / 365  [par jour]
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .deribit_client import MarketSnapshot, OptionData
from .gex import compute_gex, _compute_dte

log = logging.getLogger(__name__)

_RISK_FREE_RATE = 0.05
_MIN_IV  = 0.01
_MIN_T   = 1 / 365
_VEX_NEUTRAL_THRESH = 1_000_000    # $1M
_CEX_NEUTRAL_THRESH = 10           # 10 delta/jour


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(spot: float, strike: float, iv: float, T: float, r: float = _RISK_FREE_RATE) -> Tuple[float, float]:
    try:
        if spot <= 0 or strike <= 0 or iv <= 0 or T <= 0:
            return 0.0, 0.0
        iv_sqrt_T = iv * math.sqrt(T)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * T) / iv_sqrt_T
        return d1, d1 - iv_sqrt_T
    except (ValueError, ZeroDivisionError):
        return 0.0, 0.0


def _vanna(d1: float, d2: float, iv: float) -> float:
    if iv <= 0:
        return 0.0
    return -_norm_pdf(d1) * d2 / iv


def _charm(d1: float, d2: float, iv: float, T: float, r: float = _RISK_FREE_RATE) -> float:
    if iv <= 0 or T <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    try:
        raw = -_norm_pdf(d1) * (2 * r * T - d2 * iv * sqrt_T) / (2 * T * iv * sqrt_T)
        return raw / 365
    except ZeroDivisionError:
        return 0.0


def _fmt(val: float, decimals: int = 1) -> str:
    sign = "+" if val >= 0 else "-"
    a = abs(val)
    if a >= 1e9:
        return f"{sign}{a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}{a/1e6:.{decimals}f}M"
    if a >= 1e3:
        return f"{sign}{a/1e3:.0f}K"
    return f"{sign}{a:.0f}"


@dataclass
class VexCexProfile:
    vex_total: float
    cex_total: float
    vex_by_strike: List[dict]   # [{strike, vex}, ...] top-5 by abs
    cex_by_strike: List[dict]   # [{strike, cex}, ...] top-5 by abs
    vex_total_fmt: str
    cex_total_fmt: str
    vex_direction: str
    cex_direction: str
    vex_interpretation: str
    cex_interpretation: str
    gamma_flip: Optional[float]
    gamma_flip_dist_pct: Optional[float]
    gamma_flip_side: str
    gamma_flip_regime: str
    gamma_flip_interpretation: str
    btc_price: float
    timestamp: float


def compute_vex_cex(snapshot: MarketSnapshot) -> VexCexProfile:
    spot = snapshot.btc_price
    vex_map: Dict[float, float] = {}
    cex_map: Dict[float, float] = {}

    for opt in snapshot.options:
        dte = _compute_dte(opt.expiry)
        if dte <= 0:
            continue
        T  = max(dte / 365, _MIN_T)
        iv = max(opt.iv, _MIN_IV)
        d1, d2 = _d1_d2(spot, opt.strike, iv, T)
        van = _vanna(d1, d2, iv)
        cha = _charm(d1, d2, iv, T)

        if opt.option_type == "call":
            vex =  van * opt.oi * spot
            cex = -cha * opt.oi
        else:
            vex = -van * opt.oi * spot
            cex =  cha * opt.oi

        vex_map[opt.strike] = vex_map.get(opt.strike, 0.0) + vex
        cex_map[opt.strike] = cex_map.get(opt.strike, 0.0) + cex

    vex_total = sum(vex_map.values())
    cex_total = sum(cex_map.values())

    top_vex = sorted(vex_map.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
    top_cex = sorted(cex_map.items(), key=lambda x: abs(x[1]), reverse=True)[:5]

    vex_dir = ("BULLISH_VANNA" if vex_total >  _VEX_NEUTRAL_THRESH else
               "BEARISH_VANNA" if vex_total < -_VEX_NEUTRAL_THRESH else "NEUTRAL")
    cex_dir = ("BULLISH_CHARM" if cex_total >  _CEX_NEUTRAL_THRESH else
               "BEARISH_CHARM" if cex_total < -_CEX_NEUTRAL_THRESH else "NEUTRAL")

    vex_interp = (
        "VEX+ : une hausse de vol implicite pousse les dealers a acheter du BTC."
        if vex_total > _VEX_NEUTRAL_THRESH else
        "VEX- : une hausse de vol implicite oblige les dealers a vendre du BTC."
        if vex_total < -_VEX_NEUTRAL_THRESH else
        "VEX neutre : la vanna n'exerce pas de pression directionnelle significative."
    )
    cex_interp = (
        "CEX+ : le temps qui passe pousse les dealers a acheter du BTC."
        if cex_total > _CEX_NEUTRAL_THRESH else
        "CEX- : le temps qui passe oblige les dealers a vendre du BTC."
        if cex_total < -_CEX_NEUTRAL_THRESH else
        "CEX neutre : le charm decay n'exerce pas de pression directionnelle significative."
    )

    # Gamma flip from GEX
    gex_prof = compute_gex(snapshot)
    flip = gex_prof.flip_level
    flip_dist: Optional[float] = None
    flip_side = "none"
    if flip is not None and spot > 0:
        flip_dist = round((flip - spot) / spot * 100, 2)
        flip_side = "above" if flip > spot else "below"

    regime_map = {"STABILISANT": "STABILISATEUR", "AMPLIFICATEUR": "AMPLIFICATEUR", "NEUTRE": "NEUTRE"}
    flip_regime = regime_map.get(gex_prof.regime, gex_prof.regime)

    flip_interp = (
        f"GEX {flip_regime} : le spot est {'au-dessus' if flip_side == 'above' else 'en-dessous'} du Gamma Flip."
        if flip is not None else
        "Gamma Flip non detecte dans la zone de marche actuelle."
    )

    return VexCexProfile(
        vex_total=round(vex_total, 2),
        cex_total=round(cex_total, 4),
        vex_by_strike=[{"strike": round(k, 0), "vex": round(v, 2)} for k, v in top_vex],
        cex_by_strike=[{"strike": round(k, 0), "cex": round(v, 4)} for k, v in top_cex],
        vex_total_fmt=_fmt(vex_total),
        cex_total_fmt=_fmt(cex_total),
        vex_direction=vex_dir,
        cex_direction=cex_dir,
        vex_interpretation=vex_interp,
        cex_interpretation=cex_interp,
        gamma_flip=round(flip, 0) if flip is not None else None,
        gamma_flip_dist_pct=flip_dist,
        gamma_flip_side=flip_side,
        gamma_flip_regime=flip_regime,
        gamma_flip_interpretation=flip_interp,
        btc_price=round(spot, 2),
        timestamp=snapshot.timestamp,
    )
