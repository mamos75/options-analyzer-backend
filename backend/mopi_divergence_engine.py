"""
MOPI Divergence Engine — Moteur challenger basé sur les divergences MOPI vs prix BTC.

Logique :
  Bullish  : BTC baisse / MOPI tient (pression vendeuse options s'essouffle → rebond)
  Bearish  : BTC monte / MOPI chute  (pression acheteuse options s'essouffle → retournement)
  None     : pas de divergence → RANGE

Fenêtres : 4h / 8h / 12h / 24h (snapshots toutes les 30min).
Déduplication : épisode continu = un seul unique_setup (évite d'inflater N).
Cap confiance : jamais > 65% avant validation statistique.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

_MDE_NAME    = "mopi_divergence_engine"
_MDE_VERSION = "mopi-div-v1"

_HORIZONS        = ["4h", "24h", "72h"]
_WINDOWS_H       = [4, 8, 12, 24]        # fenêtres en heures
_SNAP_INTERVAL_H = 0.5                   # 30min entre snapshots
_DIR_THRESHOLD   = 0.5                   # 0.5% → UP/DOWN pour realized direction
_SLOPE_THRESHOLD = 0.0005                # seuil minimal de slope pour qualifier une direction
_DIV_THRESHOLD   = 0.005                 # force minimale (slopes normalisées — unités petites)
_STRONG_DIV      = 0.015                 # force forte
_MAX_CONF        = 0.65                  # cap absolu avant validation stats
_MIN_SNAPS       = 8                     # minimum de snapshots pour signal valide

# ─────────────────────────── Setup safety thresholds ─────────────────────────
_N_EXPLORATION   = 30    # < 30 unique setups → EXPLORATION, jamais promouvoir
_N_FRAGILE       = 100   # 30-99 → SIGNAL FRAGILE
# ≥ 100 → SIGNAL ROBUSTE

# Confidence caps par label
_CONF_CAP_EXPLORATION = 0.45   # forcé si N < 30
_CONF_CAP_FRAGILE     = 0.55   # forcé si 30 ≤ N < 100


# ─────────────────────────── DB helper ───────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _get_history(hours: int = 26) -> List[Dict]:
    cutoff = int(time.time()) - hours * 3600
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, mopi, btc_price FROM metrics_history "
            "WHERE ts >= ? AND mopi IS NOT NULL AND btc_price IS NOT NULL AND btc_price > 0 "
            "ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────── Feature engineering ─────────────────────────────

def _linear_slope_norm(values: List[float]) -> float:
    """Pente normalisée (% de la moyenne / step) — permet de comparer MOPI vs prix."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_v = float(np.mean(values))
    if abs(mean_v) < 1e-10:
        return 0.0
    x = np.arange(n, dtype=np.float64)
    y = np.array(values, dtype=np.float64) / mean_v
    x_m, y_m = np.mean(x), np.mean(y)
    num = float(np.sum((x - x_m) * (y - y_m)))
    den = float(np.sum((x - x_m) ** 2))
    return float(num / den) if abs(den) > 1e-12 else 0.0


def _pearson_corr(a: List[float], b: List[float]) -> float:
    if len(a) < 3 or len(b) < 3:
        return 0.0
    try:
        ax = np.array(a, dtype=np.float64)
        bx = np.array(b, dtype=np.float64)
        sa, sb = ax.std(), bx.std()
        if sa < 1e-10 or sb < 1e-10:
            return 0.0
        return float(np.mean((ax - ax.mean()) * (bx - bx.mean())) / (sa * sb))
    except Exception:
        return 0.0


def compute_divergence_features(snapshots: List[Dict], window_h: int) -> dict:
    """Calcule les features de divergence pour une fenêtre donnée."""
    n_expected = max(4, int(window_h / _SNAP_INTERVAL_H))
    snaps = snapshots[-n_expected:] if len(snapshots) >= n_expected else snapshots

    if len(snaps) < 4:
        return {"valid": False, "window": window_h, "n_snapshots": len(snaps)}

    prices = [s["btc_price"] for s in snaps]
    mopis  = [s["mopi"]      for s in snaps]
    n      = len(snaps)
    half   = max(2, n // 2)

    price_slope = _linear_slope_norm(prices)
    mopi_slope  = _linear_slope_norm(mopis)
    corr        = _pearson_corr(prices, mopis)

    prices_old, prices_new = prices[:half], prices[half:]
    mopis_old,  mopis_new  = mopis[:half],  mopis[half:]

    price_lower_low   = float(min(prices_new)) < float(min(prices_old)) * 0.999
    price_higher_high = float(max(prices_new)) > float(max(prices_old)) * 1.001
    # MOPI tient mieux : son minimum récent > ancien minimum - tolérance 2pts
    mopi_higher_low   = float(min(mopis_new)) > float(min(mopis_old)) - 2.0
    # MOPI traîne : son maximum récent < ancien maximum + tolérance 2pts
    mopi_lower_high   = float(max(mopis_new)) < float(max(mopis_old)) + 2.0

    # Divergence strength = désalignement des slopes normalisées
    strength = abs(mopi_slope - price_slope)

    return {
        "valid":                      True,
        "window":                     window_h,
        "n_snapshots":                n,
        "price_slope":                round(price_slope, 6),
        "mopi_slope":                 round(mopi_slope, 6),
        "mopi_price_correlation":     round(corr, 3),
        "mopi_divergence_strength":   round(strength, 4),
        "mopi_divergence_bullish":    price_lower_low  and mopi_higher_low,
        "mopi_divergence_bearish":    price_higher_high and mopi_lower_high,
        "price_lower_low":            price_lower_low,
        "price_higher_high":          price_higher_high,
        "mopi_higher_low":            mopi_higher_low,
        "mopi_lower_high":            mopi_lower_high,
        "last_mopi":                  round(mopis[-1], 1) if mopis else None,
        "last_price":                 round(prices[-1], 0) if prices else None,
    }


# ─────────────────────────── Divergence detection ────────────────────────────

def detect_divergence(feat: dict) -> Tuple[str, float]:
    """Retourne (type, strength) — type in ['bullish', 'bearish', 'none']."""
    if not feat.get("valid"):
        return "none", 0.0

    price_slope = feat["price_slope"]
    mopi_slope  = feat["mopi_slope"]
    strength    = feat["mopi_divergence_strength"]

    # Bullish : prix en baisse ET MOPI ne suit pas la baisse
    # Critère 1 : slope + désalignement fort
    bull_slope  = price_slope < -_SLOPE_THRESHOLD and mopi_slope > price_slope
    bull_struct = feat.get("mopi_divergence_bullish", False)
    # Critère 2 : structure structurelle seule si très claire (both higher_low + lower_low)
    bull_pure   = (feat.get("price_lower_low", False) and feat.get("mopi_higher_low", False)
                   and mopi_slope >= 0 and price_slope < 0)

    # Bearish : prix en hausse ET MOPI ne suit pas la hausse
    bear_slope  = price_slope > _SLOPE_THRESHOLD and mopi_slope < price_slope
    bear_struct = feat.get("mopi_divergence_bearish", False)
    bear_pure   = (feat.get("price_higher_high", False) and feat.get("mopi_lower_high", False)
                   and mopi_slope <= 0 and price_slope > 0)

    is_bull = (bull_slope and (bull_struct or strength >= _DIV_THRESHOLD)) or (bull_pure and strength >= _DIV_THRESHOLD)
    is_bear = (bear_slope and (bear_struct or strength >= _DIV_THRESHOLD)) or (bear_pure and strength >= _DIV_THRESHOLD)

    if not is_bull and not is_bear:
        return "none", 0.0

    if is_bull and is_bear:
        # Contradiction → choisir selon la direction du MOPI
        is_bull = mopi_slope >= 0
        is_bear = not is_bull

    div_type = "bullish" if is_bull else "bearish"
    return div_type, round(strength, 4)


def _select_best_window(snapshots: List[Dict]) -> Tuple[str, float, dict]:
    """Sélectionne la fenêtre avec la divergence la plus forte."""
    best_type, best_str, best_feat = "none", 0.0, {}
    for w in _WINDOWS_H:
        feat = compute_divergence_features(snapshots, w)
        dtype, dstr = detect_divergence(feat)
        if dtype != "none" and dstr > best_str:
            best_type, best_str, best_feat = dtype, dstr, feat
    if best_type == "none":
        # Retourner features de la plus grande fenêtre pour infos de contexte
        best_feat = compute_divergence_features(snapshots, 24)
    return best_type, best_str, best_feat


def _divergence_age(snapshots: List[Dict], div_type: str) -> int:
    """Nombre de snapshots consécutifs où la même divergence a été active (fenêtre 4h)."""
    if div_type == "none" or len(snapshots) < 8:
        return 0
    age = 0
    # Slide une fenêtre de 4h (8 snaps) vers le passé
    step = max(1, int(4 / _SNAP_INTERVAL_H))
    for end in range(len(snapshots), step - 1, -1):
        sub = snapshots[max(0, end - step * 2): end]
        feat = compute_divergence_features(sub, 4)
        dtype, dstr = detect_divergence(feat)
        if dtype == div_type and dstr >= _DIV_THRESHOLD:
            age += 1
        else:
            break
    return age


# ─────────────────────────── Probability builder ─────────────────────────────

def build_probabilities(
    div_type: str, strength: float
) -> Tuple[float, float, float, float, str]:
    """Retourne (prob_up, prob_down, prob_range, confidence, dominant)."""

    def _dom(pu, pd, pr):
        if pu > pd and pu > pr and pu > 0.38:
            return "UP"
        if pd > pu and pd > pr and pd > 0.38:
            return "DOWN"
        return "RANGE"

    if div_type == "bullish":
        if strength >= _STRONG_DIV:
            pu, pd, pr, cf = 0.60, 0.20, 0.20, 0.60
        else:
            ratio = min(1.0, strength / _STRONG_DIV)
            pu = round(0.50 + ratio * 0.10, 3)
            pd = round(0.30 - ratio * 0.08, 3)
            pr = round(max(0.10, 1.0 - pu - pd), 3)
            cf = round(0.40 + ratio * 0.12, 3)
    elif div_type == "bearish":
        if strength >= _STRONG_DIV:
            pu, pd, pr, cf = 0.20, 0.60, 0.20, 0.60
        else:
            ratio = min(1.0, strength / _STRONG_DIV)
            pd = round(0.50 + ratio * 0.10, 3)
            pu = round(0.30 - ratio * 0.08, 3)
            pr = round(max(0.10, 1.0 - pu - pd), 3)
            cf = round(0.40 + ratio * 0.12, 3)
    else:
        pu, pd, pr, cf = 0.25, 0.25, 0.50, 0.35

    # Clamp — jamais > 65% avant validation stats
    pu = round(min(_MAX_CONF, max(0.05, pu)), 3)
    pd = round(min(_MAX_CONF, max(0.05, pd)), 3)
    pr = round(max(0.0, 1.0 - pu - pd), 3)
    cf = round(min(_MAX_CONF, max(0.30, cf)), 3)

    return pu, pd, pr, cf, _dom(pu, pd, pr)


# ─────────────────────────── Setup safety labels ─────────────────────────────

def get_setup_label(n_unique: int) -> str:
    """Retourne EXPLORATION / SIGNAL FRAGILE / SIGNAL ROBUSTE selon N unique setups."""
    if n_unique < _N_EXPLORATION:
        return "EXPLORATION"
    if n_unique < _N_FRAGILE:
        return "SIGNAL FRAGILE"
    return "SIGNAL ROBUSTE"


def apply_setup_gate(confidence: float, n_unique: int) -> Tuple[float, str, bool]:
    """Applique le cap de confiance selon le label de sécurité.

    Retourne (confidence_ajustée, setup_label, can_promote).
    Règle : jamais promouvoir avant N unique setups >= 30.
    """
    label = get_setup_label(n_unique)
    if n_unique < _N_EXPLORATION:
        return round(min(confidence, _CONF_CAP_EXPLORATION), 3), label, False
    if n_unique < _N_FRAGILE:
        return round(min(confidence, _CONF_CAP_FRAGILE), 3), label, True
    return round(min(confidence, _MAX_CONF), 3), label, True


# ─────────────────────────── Setup deduplication ─────────────────────────────

def compute_unique_setup_count(model_name: str, horizon: str = "4h", days: int = 30) -> dict:
    """Déduplique les épisodes continus.

    Retourne raw_event_count + unique_setup_count.
    Une divergence haussière qui dure 6h (12 prédictions 30min) = 1 setup unique.
    """
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            """SELECT mp.timestamp, mp.explanation_json
               FROM model_predictions mp
               WHERE mp.model_name = ? AND mp.horizon = ? AND mp.timestamp >= ?
               ORDER BY mp.timestamp ASC""",
            (model_name, horizon, cutoff),
        ).fetchall()

    raw_count = len(rows)
    if raw_count == 0:
        return {"raw_event_count": 0, "unique_setup_count": 0}

    # Extraire le type de divergence par prédiction
    type_seq: List[str] = []
    for r in rows:
        try:
            expl = json.loads(r["explanation_json"] or "{}")
            type_seq.append(expl.get("divergence_type", "none"))
        except Exception:
            type_seq.append("none")

    # Compter les transitions (changement de type = nouveau setup)
    # On ignore les "none" comme séparateurs naturels
    unique = 0
    prev_type = None
    for t in type_seq:
        if t != "none":
            if t != prev_type:
                unique += 1
            prev_type = t
        else:
            prev_type = None  # reset après un épisode "none"

    label = get_setup_label(unique)
    return {
        "raw_event_count":    raw_count,
        "unique_setup_count": unique,
        "setup_label":        label,
        "can_promote":        unique >= _N_EXPLORATION,
    }


# ─────────────────────────── Current signal endpoint helper ──────────────────

def get_current_mde_signal() -> dict:
    """Snapshot complet pour l'endpoint /api/mopi_divergence."""
    snapshots = _get_history(hours=26)
    n_snaps   = len(snapshots)

    if n_snaps < _MIN_SNAPS:
        return {
            "divergence_type":    "none",
            "strength":           0.0,
            "window":             None,
            "last_mopi":          snapshots[-1]["mopi"] if snapshots else None,
            "last_price":         snapshots[-1]["btc_price"] if snapshots else None,
            "prob_up":            0.25,
            "prob_down":          0.25,
            "prob_range":         0.50,
            "confidence":         0.35,
            "dominant_scenario":  "RANGE",
            "divergence_age":     0,
            "n_snapshots":        n_snaps,
            "features":           {},
            "message":            f"Données insuffisantes — {n_snaps} snapshots (min {_MIN_SNAPS})",
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    div_type, strength, best_feat = _select_best_window(snapshots)
    age  = _divergence_age(snapshots, div_type)
    pu, pd, pr, cf, dom = build_probabilities(div_type, strength)

    # Setup stats par horizon
    setup_stats = {}
    for hz in _HORIZONS:
        setup_stats[hz] = compute_unique_setup_count(_MDE_NAME, hz, days=30)

    # Gate de sécurité : N unique setups (référence horizon 4h)
    n_unique   = setup_stats["4h"]["unique_setup_count"]
    cf, setup_label, can_promote = apply_setup_gate(cf, n_unique)

    # Dernier signal détecté (première prédiction non-none)
    last_signal_ts = None
    with _conn() as c:
        row = c.execute(
            """SELECT timestamp, explanation_json FROM model_predictions
               WHERE model_name = ? AND horizon = '4h'
               ORDER BY timestamp DESC LIMIT 50""",
            (_MDE_NAME,),
        ).fetchall()
    for r in (row or []):
        try:
            expl = json.loads(r["explanation_json"] or "{}")
            if expl.get("divergence_type") not in (None, "none"):
                last_signal_ts = r["timestamp"]
                break
        except Exception:
            pass

    features_public = {k: v for k, v in best_feat.items()
                       if k not in ("valid", "last_price")}

    if div_type == "none":
        message = "Pas de divergence exploitable actuellement."
    elif div_type == "bullish":
        message = (f"Divergence haussière détectée (force {strength:.2f}) "
                   f"sur {best_feat.get('window')}h — pression vendeuse options s'essouffle.")
    else:
        message = (f"Divergence baissière détectée (force {strength:.2f}) "
                   f"sur {best_feat.get('window')}h — pression acheteuse options s'essouffle.")

    # Warning explicite si EXPLORATION
    safety_warning = None
    if setup_label == "EXPLORATION":
        safety_warning = (
            f"EXPLORATION — {n_unique} setups uniques (min {_N_EXPLORATION} requis). "
            "Signal non validé, confidence plafonnée à 45%."
        )
    elif setup_label == "SIGNAL FRAGILE":
        safety_warning = (
            f"SIGNAL FRAGILE — {n_unique} setups uniques (min {_N_FRAGILE} pour SIGNAL ROBUSTE). "
            "Confidence plafonnée à 55%."
        )

    return {
        "divergence_type":    div_type,
        "strength":           strength,
        "window":             f"{best_feat.get('window')}h" if best_feat.get("window") else None,
        "last_mopi":          best_feat.get("last_mopi"),
        "last_price":         snapshots[-1]["btc_price"] if snapshots else None,
        "prob_up":            pu,
        "prob_down":          pd,
        "prob_range":         pr,
        "confidence":         cf,
        "dominant_scenario":  dom,
        "divergence_age":     age,
        "n_snapshots":        n_snaps,
        "features":           features_public,
        "setup_stats":        setup_stats,
        "setup_label":        setup_label,
        "n_unique_setups":    n_unique,
        "can_promote":        can_promote,
        "safety_warning":     safety_warning,
        "last_signal_ts":     last_signal_ts,
        "message":            message,
        "ts":                 datetime.now(timezone.utc).isoformat(),
        "model_name":         _MDE_NAME,
        "model_version":      _MDE_VERSION,
    }
