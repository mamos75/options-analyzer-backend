"""
Model Arena — Compétition de moteurs de prédiction en parallèle.

3 moteurs, format de sortie identique, comparaison performance continue.
Moteur principal = Expert Rules jusqu'à preuve de supériorité d'un challenger.

Critères de promotion (challenger → principal) :
  - ≥ 100 signaux évalués
  - ≥ 14 jours de données
  - EV positif
  - Winrate supérieur au moteur expert
  - Stabilité sur plusieurs horizons

Auto-calibration : poids ajustés max ±10% par semaine, bornés [0.5, 1.5].
ML Research : observation uniquement jusqu'à ≥ 30 outcomes, puis k-NN empirique.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .probability_engine import (
    compute_probability_engine,
    ScenarioProbability,
    ProbabilityEngineOutput,
)
from .btc_momentum_engine import get_bme

log = logging.getLogger(__name__)

# ─────────────────────────── Constants ───────────────────────────────────────

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

_EXPERT_NAME    = "expert_rules"
_AUTOCAL_NAME   = "auto_calibrated"
_ML_NAME        = "ml_research"

_EXPERT_VERSION  = "expert-v1"
_AUTOCAL_VERSION = "auto-calibrated-v1"
_ML_VERSION      = "ml-research-v1"

_HORIZONS = ["4h", "24h", "72h"]

_EXPERT2_NAME    = "expert_v2_crash_gate"
_EXPERT2_VERSION = "expert-v2.0"
_NAIVE_NAME      = "naive_baseline"
_NAIVE_VERSION   = "baseline-v1.0"
_ACRS_NAME       = "auto_calibrated_regime_shadow"
_ACRS_VERSION    = "acrs-v1"
_AME_NAME        = "adaptive_memory_engine"
_AME_VERSION     = "ame-v1.0"

_PRIMARY_MODELS = [_EXPERT_NAME, _EXPERT2_NAME, _NAIVE_NAME]

# Promotion thresholds spécifiques à l'AME
_AME_MIN_OUTCOMES_4H  = 300
_AME_MIN_OUTCOMES_24H = 150
_AME_MIN_OUTCOMES_72H = 75
_AME_MIN_PROFIT_FACTOR = 1.2

_MIN_OUTCOMES_PROMOTION  = 100
_MIN_DAYS_PROMOTION      = 14
_EV_THRESHOLD            = 0.0
_MIN_WINRATE_PROMOTION   = 0.52
_MIN_OUTCOMES_ML         = 30
_MIN_OUTCOMES_COLLECTING = 30

_MAX_WEIGHT_WEEKLY_CHANGE = 0.10   # ±10%/semaine max
_WEIGHT_MIN = 0.50
_WEIGHT_MAX = 1.50

_DIR_THRESHOLD_PCT = 0.5   # 0.5% → UP/DOWN, sinon RANGE

# ─────────────────────────── MOPI Divergence Engine Constants ────────────────

_MDE_NAME    = "mopi_divergence_engine"
_MDE_VERSION = "mopi-div-v1"

# Statut baseline_smart — challenger direct du Naive Baseline
_MDE_MIN_OUTCOMES_PROMOTION = 100

# ─────────────────────────── Neural Engine Constants ─────────────────────────

_NEURAL_TAB_NAME      = "neural_tabular_engine"
_NEURAL_TAB_VERSION   = "neural-tabular-v1.1"   # v1.1 — +3 MOPI div features
_TEMPORAL_NAME        = "temporal_neural_engine"
_TEMPORAL_VERSION     = "temporal-neural-v1.1"   # v1.1 — +2 MOPI div features

_NEURAL_TAB_WARMUP    = 300    # N < 300   → WARMING_UP
_NEURAL_TAB_SHADOW    = 1000   # N 300–999 → shadow (confidence cap 60%)
_NEURAL_CONF_CAP_LOW  = 0.60
_NEURAL_CONF_CAP_HIGH = 0.75
_NEURAL_CONF_MAX      = 0.85   # jamais > 85% sans calibration validée

_TEMPORAL_WARMUP      = 500    # N < 500   → WARMING_UP
_TEMPORAL_SHADOW      = 1500   # N 500–1499 → shadow (confidence cap 60%)
_TEMPORAL_CONF_CAP_LOW  = 0.60
_TEMPORAL_CONF_CAP      = 0.70  # plafond même actif

_NEURAL_MODEL_DIR     = os.environ.get("NEURAL_MODEL_DIR", "/data/neural_models")
_NEURAL_SEQ_LEN       = 24    # 24 snapshots ≈ 12h à intervalle 30min
_NEURAL_TAB_FEATURES  = 17    # dim vecteur tabulaire (14 + 3 MOPI div)
_TEMPORAL_FEATURES    = 10    # dim vecteur par timestep (8 + 2 MOPI div)
_NEURAL_RETRAIN_H     = 24    # retrain toutes les 24h max

# ─────────────────────────── BTC Momentum Engine Constants ───────────────────

_BME_NAME    = "btc_momentum_engine"
_BME_VERSION = "bme-v1.0"


# ─────────────────────────── Dataclass ───────────────────────────────────────

@dataclass
class ArenaOutput:
    model_name: str
    version: str
    timestamp: str
    horizon: str
    spot_at_prediction: float
    prob_up: float
    prob_down: float
    prob_range: float
    confidence: float
    dominant_scenario: str      # "UP" | "DOWN" | "RANGE"
    data_coverage: float
    top_factors: List[str]
    warnings: List[str]
    features_snapshot: dict = field(default_factory=dict)
    explanation: dict = field(default_factory=dict)


# ─────────────────────────── Rule → Factor Group ─────────────────────────────

_RULE_GROUP: Dict[str, str] = {
    "dex_bearish_4h": "dex", "dex_bullish_4h": "dex",
    "dex_actionable_bullish_4h": "dex", "dex_actionable_bearish_4h": "dex",
    "dex_bearish_24h": "dex", "dex_bullish_24h": "dex",
    "gex_near_negative_4h": "gex", "gex_near_positive_4h": "gex",
    "gex_momentum_4h": "gex", "gex_momentum_expansion_4h": "gex",
    "gex_near_negative_24h": "gex", "gex_near_positive_24h": "gex",
    "gex_momentum_contraction_24h": "gex", "gex_momentum_expansion_24h": "gex",
    "gex_near_negative_penalty_24h": "gex",
    "gex_amplifier_bearish_72h": "gex", "gex_stabilisant_72h": "gex",
    "gex_amplifier_bearish_72h_penalty": "gex",
    "spot_below_flip_4h": "flip", "spot_above_flip_4h": "flip",
    "spot_below_flip_24h": "flip", "spot_above_flip_24h": "flip",
    "spot_far_above_flip_24h": "flip", "spot_far_below_flip_24h": "flip",
    "iv_spike_4h": "iv", "iv_low_calm_4h": "iv",
    "iv_rising_24h": "iv", "iv_calm_24h": "iv",
    "iv_high_structural_72h": "iv", "iv_calm_structural_72h": "iv",
    "pcr_near_bearish_4h": "pcr", "pcr_near_bullish_4h": "pcr",
    "puts_skew_24h": "pcr", "calls_skew_24h": "pcr",
    "put_wall_near_support_4h": "walls", "call_wall_near_resistance_4h": "walls",
    "put_wall_near_support_24h": "walls", "call_wall_near_resistance_24h": "walls",
    "put_wall_strong_support_24h": "walls",
    "put_wall_below_structural_72h": "walls", "call_wall_above_strong_72h": "walls",
    "call_wall_above_structural_72h": "walls", "put_wall_strong_floor_72h": "walls",
    "max_pain_below_4h": "max_pain", "max_pain_above_4h": "max_pain",
    "max_pain_above_near_dte_24h": "max_pain", "max_pain_below_near_dte_24h": "max_pain",
    "max_pain_below_72h": "max_pain", "max_pain_above_72h": "max_pain",
    "max_pain_above_72h_bull": "max_pain", "max_pain_below_72h_penalty": "max_pain",
    "funding_not_negative_24h": "funding",
    "volume_not_confirming_24h": "volume",
    "futures_oi_divergence_24h": "futures_oi",
    "mopi_bearish_72h": "mopi", "mopi_bullish_72h": "mopi",
    "mopi_bullish_structural_72h": "mopi", "mopi_bearish_72h_penalty": "mopi",
    "mopi_div_bullish_4h": "mopi_div", "mopi_div_bearish_4h": "mopi_div",
    "mopi_div_bullish_24h": "mopi_div", "mopi_div_bearish_24h": "mopi_div",
    "mopi_div_bullish_72h": "mopi_div", "mopi_div_bearish_72h": "mopi_div",
}

_FACTOR_GROUPS = [
    "gex", "dex", "flip", "iv", "pcr",
    "walls", "max_pain", "funding", "volume", "futures_oi", "mopi", "mopi_div",
]

# Keys in features_json that indicate a feature group is present
_FEATURE_KEYS: Dict[str, List[str]] = {
    "gex":        ["gex_near", "gex_regime"],
    "dex":        ["dex_direction"],
    "flip":       ["flip_level", "flip_distance_pct"],
    "iv":         ["iv_rank"],
    "pcr":        ["pc_ratio_near"],
    "walls":      ["put_wall", "call_wall"],
    "max_pain":   ["max_pain_strike"],
    "funding":    ["funding_rate"],
    "volume":     ["spot_volume_24h"],
    "futures_oi": ["futures_oi"],
    "mopi":       ["mopi_score"],
    "mopi_div":   ["mopi_div_type_enc", "mopi_div_strength"],
}


# ─────────────────────────── Database ────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_arena_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_predictions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp         INTEGER NOT NULL,
                model_name        TEXT NOT NULL,
                model_version     TEXT NOT NULL,
                horizon           TEXT NOT NULL,
                spot_at_prediction REAL,
                prob_up           REAL,
                prob_down         REAL,
                prob_range        REAL,
                confidence        REAL,
                dominant_scenario TEXT,
                data_coverage     REAL,
                features_json     TEXT,
                explanation_json  TEXT,
                created_at        INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mp_ts ON model_predictions(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mp_model ON model_predictions(model_name, horizon)")
        # Migration: colonne is_seed (0 = live, 1 = artificiel) — toutes les lignes existantes = live
        try:
            c.execute("ALTER TABLE model_predictions ADD COLUMN is_seed INTEGER NOT NULL DEFAULT 0")
            c.commit()
        except Exception:
            pass  # colonne déjà présente

        c.execute("""
            CREATE TABLE IF NOT EXISTS model_outcomes (
                prediction_id              INTEGER NOT NULL,
                horizon                    TEXT NOT NULL,
                spot_entry                 REAL,
                spot_exit                  REAL,
                return_pct                 REAL,
                realized_direction         TEXT,
                is_correct                 INTEGER,
                mae                        REAL,
                mfe                        REAL,
                evaluated_at               INTEGER,
                direction_adjusted_return  REAL,
                PRIMARY KEY (prediction_id, horizon)
            )
        """)
        # Migration: add direction_adjusted_return to existing tables
        try:
            c.execute("ALTER TABLE model_outcomes ADD COLUMN direction_adjusted_return REAL")
            c.commit()
        except Exception:
            pass  # column already present

        c.execute("""
            CREATE TABLE IF NOT EXISTS arena_weights (
                group_name        TEXT NOT NULL,
                model_name        TEXT NOT NULL,
                horizon           TEXT NOT NULL,
                weight_multiplier REAL NOT NULL DEFAULT 1.0,
                last_updated      INTEGER,
                weekly_delta      REAL DEFAULT 0.0,
                PRIMARY KEY (group_name, model_name, horizon)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS arena_performance_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            INTEGER NOT NULL,
                model_name    TEXT NOT NULL,
                horizon       TEXT NOT NULL,
                window        TEXT NOT NULL,
                n_outcomes    INTEGER NOT NULL DEFAULT 0,
                winrate       REAL,
                ev_mean       REAL,
                profit_factor REAL,
                avg_win       REAL,
                avg_loss      REAL,
                created_at    INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_aph_ts    ON arena_performance_history(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_aph_model ON arena_performance_history(model_name, horizon, window)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS adaptive_memory_clusters (
                cluster_id          TEXT NOT NULL,
                horizon             TEXT NOT NULL,
                regime              TEXT,
                feature_signature   TEXT,
                n_outcomes          INTEGER NOT NULL DEFAULT 0,
                winrate             REAL,
                ev_mean             REAL,
                profit_factor       REAL,
                avg_win             REAL,
                avg_loss            REAL,
                reliability_score   REAL NOT NULL DEFAULT 1.0,
                last_updated        INTEGER,
                PRIMARY KEY (cluster_id, horizon)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_amc_cluster ON adaptive_memory_clusters(cluster_id, horizon)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS neural_training_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name  TEXT NOT NULL,
                horizon     TEXT NOT NULL,
                trained_at  INTEGER NOT NULL,
                n_samples   INTEGER,
                val_winrate REAL,
                notes       TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ntl_model ON neural_training_log(model_name, horizon, trained_at)")
        # Migrations — new observability columns
        for _col, _def in [
            ("duration_s", "REAL"),
            ("val_ev",     "REAL"),
            ("val_pf",     "REAL"),
            ("status",     "TEXT DEFAULT 'success'"),
        ]:
            try:
                c.execute(f"ALTER TABLE neural_training_log ADD COLUMN {_col} {_def}")
            except Exception:
                pass  # already present
        # Attempt log — tracks every retrain attempt including failures/skips
        c.execute("""
            CREATE TABLE IF NOT EXISTS neural_retrain_attempts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name   TEXT NOT NULL,
                horizon      TEXT NOT NULL,
                attempted_at INTEGER NOT NULL,
                status       TEXT NOT NULL,
                error_msg    TEXT,
                n_outcomes   INTEGER,
                duration_s   REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_nra ON neural_retrain_attempts(model_name, horizon, attempted_at)")
        c.commit()


# ─────────────────────────── Probability Helpers ─────────────────────────────

def _to_prob3(bull_pct: float, bear_pct: float) -> Tuple[float, float, float]:
    """Convert bull/bear probabilities [5-95%] to (prob_up, prob_down, prob_range) summing ~1."""
    b = max(5.0, min(95.0, bull_pct)) / 100.0
    d = max(5.0, min(95.0, bear_pct)) / 100.0
    max_signal = max(b, d)
    range_share = max(0.0, 1.0 - max_signal - 0.10)
    directional = 1.0 - range_share
    total_raw = b + d
    if total_raw > 0:
        pb = directional * b / total_raw
        pd = directional * d / total_raw
    else:
        pb = pd = directional / 2.0
    pr = max(0.0, 1.0 - pb - pd)
    return round(pb, 3), round(pd, 3), round(pr, 3)


def _dominant_from_prob3(prob_up: float, prob_down: float, prob_range: float) -> str:
    if prob_up > prob_down and prob_up > prob_range and prob_up > 0.38:
        return "UP"
    if prob_down > prob_up and prob_down > prob_range and prob_down > 0.38:
        return "DOWN"
    return "RANGE"


def _extract_rules_info(bull: ScenarioProbability, bear: ScenarioProbability) -> List[dict]:
    rules = []
    seen = set()
    for r, direction in [
        *[(r, "bull") for r in bull.positive_rules + bull.penalty_rules],
        *[(r, "bear") for r in bear.positive_rules + bear.penalty_rules],
    ]:
        key = (r.id, direction)
        if key in seen or r.pts_applied == 0:
            continue
        seen.add(key)
        rules.append({
            "id": r.id,
            "pts_applied": r.pts_applied,
            "group": _RULE_GROUP.get(r.id, "other"),
            "direction": direction,
        })
    return rules


def _apply_multipliers_to_scenario(
    scenario: ScenarioProbability,
    multipliers: Dict[str, float],
) -> float:
    """Recompute scenario probability with group weight multipliers applied."""
    new_pts = 0.0
    for r in scenario.positive_rules + scenario.penalty_rules:
        group = _RULE_GROUP.get(r.id, "other")
        mult = multipliers.get(group, 1.0)
        new_pts += r.pts_applied * mult
    raw = 50.0 + new_pts
    return max(5.0, min(95.0, raw))


def _scenario_pair_to_arena(
    bull: ScenarioProbability,
    bear: ScenarioProbability,
    horizon: str,
    model_name: str,
    version: str,
    spot: float,
    weight_multipliers: Optional[Dict[str, float]] = None,
) -> ArenaOutput:
    if weight_multipliers:
        bull_p = _apply_multipliers_to_scenario(bull, weight_multipliers)
        bear_p = _apply_multipliers_to_scenario(bear, weight_multipliers)
    else:
        bull_p = bull.probability
        bear_p = bear.probability

    prob_up, prob_down, prob_range = _to_prob3(bull_p, bear_p)
    dominant = _dominant_from_prob3(prob_up, prob_down, prob_range)
    conf = (bull.confidence + bear.confidence) / 2.0 / 100.0

    top: List[str] = []
    seen: set = set()
    for c in (bull.top_contributors or []) + (bear.top_contributors or []):
        if c not in seen:
            top.append(c)
            seen.add(c)
    top = top[:4]

    warnings = []
    if bull.crash_regime_active:
        warnings.append("Crash regime gate actif — BULL plafonné à 55%")
    if (bull.data_coverage_pct or 0) < 50 or (bear.data_coverage_pct or 0) < 50:
        warnings.append("Couverture données < 50% — signal dégradé")

    explanation = {
        "bull_probability": round(bull_p, 1),
        "bear_probability": round(bear_p, 1),
        "bull_confidence": round(bull.confidence, 1),
        "bear_confidence": round(bear.confidence, 1),
        "bull_top": bull.top_contributors,
        "bear_top": bear.top_contributors,
        "rules": _extract_rules_info(bull, bear),
    }
    if weight_multipliers:
        explanation["weight_multipliers"] = weight_multipliers

    coverage = ((bull.data_coverage_pct or 0) + (bear.data_coverage_pct or 0)) / 200.0

    return ArenaOutput(
        model_name=model_name,
        version=version,
        timestamp=datetime.now(timezone.utc).isoformat(),
        horizon=horizon,
        spot_at_prediction=spot,
        prob_up=prob_up,
        prob_down=prob_down,
        prob_range=prob_range,
        confidence=round(conf, 3),
        dominant_scenario=dominant,
        data_coverage=round(coverage, 3),
        top_factors=top,
        warnings=warnings,
        explanation=explanation,
    )


# ─────────────────────────── MOPI Divergence nudge (expert engines) ──────────

_DIV_MIN_STRENGTH = 0.005   # force minimale pour activer le nudge
_DIV_MAX_NUDGE    = 0.025   # nudge max = 2.5% de probabilité


def _apply_divergence_nudge(out: ArenaOutput, features: dict) -> ArenaOutput:
    """Applique un petit ajustement de probabilité basé sur la divergence MOPI/prix.

    Bullish divergence → +nudge sur prob_up.
    Bearish divergence → +nudge sur prob_down.
    Renormalise et met à jour dominant_scenario.
    """
    div_type = float(features.get("mopi_div_type_enc", 0.0) or 0.0)
    div_str  = float(features.get("mopi_div_strength", 0.0) or 0.0)
    if abs(div_type) < 0.5 or div_str < _DIV_MIN_STRENGTH:
        return out
    ratio = min(1.0, (div_str - _DIV_MIN_STRENGTH) / 0.010)
    nudge = ratio * _DIV_MAX_NUDGE
    pu, pd, pr = out.prob_up, out.prob_down, out.prob_range
    if div_type > 0:
        pu = pu + nudge
        pr = max(0.0, pr - nudge)
        div_tag = "mopi_div_bullish"
    else:
        pd = pd + nudge
        pr = max(0.0, pr - nudge)
        div_tag = "mopi_div_bearish"
    total = pu + pd + pr
    if total < 1e-6:
        return out
    pu, pd, pr = pu / total, pd / total, pr / total
    out.prob_up            = round(pu, 3)
    out.prob_down          = round(pd, 3)
    out.prob_range         = round(pr, 3)
    out.dominant_scenario  = _dominant_from_prob3(pu, pd, pr)
    out.top_factors        = ([div_tag] + out.top_factors)[:4]
    out.explanation["mopi_div_nudge"] = round(nudge, 4)
    return out


# ─────────────────────────── DB helpers ──────────────────────────────────────

def _save_prediction(output: ArenaOutput) -> int:
    ts = int(time.time())
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO model_predictions
            (timestamp, model_name, model_version, horizon,
             spot_at_prediction, prob_up, prob_down, prob_range,
             confidence, dominant_scenario, data_coverage,
             features_json, explanation_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts, output.model_name, output.version, output.horizon,
                output.spot_at_prediction,
                output.prob_up, output.prob_down, output.prob_range,
                output.confidence, output.dominant_scenario, output.data_coverage,
                json.dumps(output.features_snapshot, ensure_ascii=False),
                json.dumps(output.explanation, ensure_ascii=False),
                ts,
            ),
        )
        c.commit()
        return cur.lastrowid


def _get_weight_multipliers(model_name: str, horizon: str) -> Dict[str, float]:
    with _conn() as c:
        rows = c.execute(
            "SELECT group_name, weight_multiplier FROM arena_weights WHERE model_name=? AND horizon=?",
            (model_name, horizon),
        ).fetchall()
    result = {g: 1.0 for g in _FACTOR_GROUPS}
    for r in rows:
        result[r["group_name"]] = float(r["weight_multiplier"])
    return result


def _update_weight_multiplier(model_name: str, horizon: str, group: str, delta: float):
    now = int(time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT weight_multiplier, weekly_delta FROM arena_weights "
            "WHERE group_name=? AND model_name=? AND horizon=?",
            (group, model_name, horizon),
        ).fetchone()
        current = float(row["weight_multiplier"]) if row else 1.0
        weekly = float(row["weekly_delta"]) if row else 0.0

        remaining = _MAX_WEIGHT_WEEKLY_CHANGE - abs(weekly)
        if remaining <= 0:
            return
        actual_delta = max(-remaining, min(remaining, delta))
        new_mult = max(_WEIGHT_MIN, min(_WEIGHT_MAX, current + actual_delta))

        c.execute(
            """INSERT OR REPLACE INTO arena_weights
            (group_name, model_name, horizon, weight_multiplier, last_updated, weekly_delta)
            VALUES (?,?,?,?,?,?)""",
            (group, model_name, horizon, new_mult, now, weekly + actual_delta),
        )
        c.commit()


def reset_weekly_deltas():
    """Réinitialise les compteurs de delta hebdomadaire (à appeler chaque lundi)."""
    with _conn() as c:
        c.execute("UPDATE arena_weights SET weekly_delta = 0.0")
        c.commit()


# ─────────────────────────── Expert Rules Engine ─────────────────────────────

class ExpertRulesEngine:
    name = _EXPERT_NAME
    version = _EXPERT_VERSION

    def predict(
        self,
        spot: float,
        pe_output: ProbabilityEngineOutput,
        features_snapshot: dict = None,
    ) -> List[ArenaOutput]:
        pairs = [
            (pe_output.bull_4h,  pe_output.bear_4h,  "4h"),
            (pe_output.bull_24h, pe_output.bear_24h, "24h"),
            (pe_output.bull_72h, pe_output.bear_72h, "72h"),
        ]
        results = [
            _scenario_pair_to_arena(bull, bear, hz, self.name, self.version, spot)
            for bull, bear, hz in pairs
        ]
        if features_snapshot:
            results = [_apply_divergence_nudge(out, features_snapshot) for out in results]
        return results


# ─────────────────────────── Auto-Calibrated Engine ──────────────────────────

class AutoCalibratedEngine:
    name = _AUTOCAL_NAME
    version = _AUTOCAL_VERSION

    def predict(
        self,
        spot: float,
        pe_output: ProbabilityEngineOutput,
        features_snapshot: dict = None,
    ) -> List[ArenaOutput]:
        pairs = [
            (pe_output.bull_4h,  pe_output.bear_4h,  "4h"),
            (pe_output.bull_24h, pe_output.bear_24h, "24h"),
            (pe_output.bull_72h, pe_output.bear_72h, "72h"),
        ]
        results = []
        for bull, bear, hz in pairs:
            mults = _get_weight_multipliers(self.name, hz)
            out = _scenario_pair_to_arena(bull, bear, hz, self.name, self.version, spot, mults)
            if features_snapshot:
                out = _apply_divergence_nudge(out, features_snapshot)
            results.append(out)
        return results

    def calibrate_from_outcome(self, horizon: str, is_correct: bool, explanation: dict):
        """Ajuste les poids de groupes selon le résultat d'une prédiction."""
        rules = explanation.get("rules", [])
        if not rules:
            return

        bull_p = explanation.get("bull_probability", 50)
        bear_p = explanation.get("bear_probability", 50)
        predicted_bull = bull_p > bear_p

        for rule in rules:
            group = rule.get("group", "other")
            pts = rule.get("pts_applied", 0)
            if pts == 0 or group == "other":
                continue

            rule_dir = rule.get("direction", "")
            rule_aligned = (rule_dir == "bull" and predicted_bull and pts > 0) or \
                           (rule_dir == "bear" and not predicted_bull and pts > 0)

            if is_correct:
                delta = 0.01 if rule_aligned else 0.0
            else:
                delta = -0.03 if rule_aligned else 0.01

            if delta != 0:
                _update_weight_multiplier(self.name, horizon, group, delta)


# ─────────────────────────── ML Research Engine ──────────────────────────────

class MLResearchEngine:
    name = _ML_NAME
    version = _ML_VERSION

    def _get_training_data(self, horizon: str) -> Tuple[List[dict], List[str]]:
        """Récupère features + outcomes pour l'entraînement (basé sur prédictions expert)."""
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.features_json, mo.realized_direction
                   FROM model_predictions mp
                   JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = ? AND mp.horizon = ?
                     AND mo.realized_direction IS NOT NULL
                   ORDER BY mp.timestamp DESC LIMIT 500""",
                (_EXPERT_NAME, horizon),
            ).fetchall()
        features, labels = [], []
        for r in rows:
            try:
                feat = json.loads(r["features_json"] or "{}")
                if feat:
                    features.append(feat)
                    labels.append(r["realized_direction"])
            except Exception:
                pass
        return features, labels

    def _to_vector(self, f: dict) -> Optional[np.ndarray]:
        try:
            gex_near = float(f.get("gex_near", 0) or 0)
            dex_dir  = f.get("dex_direction", "")
            dex_enc  = 1.0 if dex_dir == "BULLISH_FLOWS" else (-1.0 if dex_dir == "BEARISH_FLOWS" else 0.0)
            iv_rank  = float(f.get("iv_rank", 50) or 50)
            pcr_near = float(f.get("pc_ratio_near", 1.0) or 1.0)
            mopi     = float(f.get("mopi_score", 50) or 50)
            flip_d   = float(f.get("flip_distance_pct", 0) or 0)
            regime   = f.get("gex_regime", "NEUTRE")
            regime_enc = 1.0 if regime == "STABILISANT" else (-1.0 if regime == "AMPLIFICATEUR" else 0.0)
            div_type = float(f.get("mopi_div_type_enc", 0.0) or 0.0)
            div_str  = float(f.get("mopi_div_strength", 0.0) or 0.0)
            div_corr = float(f.get("mopi_price_corr", 0.0) or 0.0)
            return np.array([
                np.clip(gex_near / 1e9, -5.0, 5.0),
                dex_enc,
                (iv_rank - 50.0) / 50.0,
                np.clip(pcr_near - 1.0, -2.0, 2.0),
                (mopi - 50.0) / 50.0,
                np.clip(flip_d, -0.20, 0.20),
                regime_enc,
                float(np.clip(div_type, -1.0, 1.0)),
                float(np.clip(div_str / 0.030, 0.0, 1.0)),
                float(np.clip(div_corr, -1.0, 1.0)),
            ], dtype=np.float64)
        except Exception:
            return None

    def _knn_predict(
        self,
        features_hist: List[dict],
        labels_hist: List[str],
        current_features: dict,
    ) -> Tuple[float, float, float, float, dict]:
        """k-NN empirique — retourne (prob_up, prob_down, prob_range, confidence, diagnostics)."""
        _empty_diag: dict = {"n_neighbors_used": 0, "n_outcomes_available": 0, "avg_neighbor_distance": None, "neighbor_distribution": {"UP": 0, "DOWN": 0, "RANGE": 0}}
        if len(features_hist) < _MIN_OUTCOMES_ML:
            return 0.333, 0.333, 0.333, 0.0, _empty_diag

        curr_vec = self._to_vector(current_features)
        if curr_vec is None:
            return 0.333, 0.333, 0.333, 0.0, _empty_diag

        hist_vecs, hist_labels = [], []
        for f, l in zip(features_hist, labels_hist):
            v = self._to_vector(f)
            if v is not None:
                hist_vecs.append(v)
                hist_labels.append(l)

        n_available = len(hist_vecs)
        if n_available < _MIN_OUTCOMES_ML:
            return 0.333, 0.333, 0.333, 0.0, _empty_diag

        X = np.array(hist_vecs)
        # Normalisation L2
        norms_x = np.linalg.norm(X, axis=1, keepdims=True)
        norms_x = np.where(norms_x < 1e-10, 1e-10, norms_x)
        X_n = X / norms_x

        norm_c = np.linalg.norm(curr_vec)
        if norm_c < 1e-10:
            return 0.333, 0.333, 0.333, 0.0, _empty_diag
        curr_n = curr_vec / norm_c

        sims = X_n @ curr_n
        k = max(5, min(20, n_available // 5))
        top_idx = np.argsort(sims)[-k:]

        top_labels = [hist_labels[i] for i in top_idx]
        top_sims = np.clip(sims[top_idx] + 1.0, 0.0, 2.0)

        up_w    = sum(w for w, l in zip(top_sims, top_labels) if l == "UP")
        down_w  = sum(w for w, l in zip(top_sims, top_labels) if l == "DOWN")
        range_w = sum(w for w, l in zip(top_sims, top_labels) if l == "RANGE")

        # Laplace smoothing — évite les probabilités dégénérées 100%/0%
        _LAPLACE = 1.0
        up_w    += _LAPLACE
        down_w  += _LAPLACE
        range_w += _LAPLACE
        total_w  = up_w + down_w + range_w

        conf = float(np.mean(np.clip(sims[top_idx], 0.0, 1.0)))
        avg_dist = round(float(np.mean(1.0 - np.clip(sims[top_idx], -1.0, 1.0))), 4)

        diag: dict = {
            "n_neighbors_used": int(k),
            "n_outcomes_available": int(n_available),
            "avg_neighbor_distance": avg_dist,
            "neighbor_distribution": {
                "UP":    sum(1 for l in top_labels if l == "UP"),
                "DOWN":  sum(1 for l in top_labels if l == "DOWN"),
                "RANGE": sum(1 for l in top_labels if l == "RANGE"),
            },
        }

        return (
            round(float(up_w    / total_w), 3),
            round(float(down_w  / total_w), 3),
            round(float(range_w / total_w), 3),
            round(conf, 3),
            diag,
        )

    def predict(self, spot: float, current_features: dict) -> List[ArenaOutput]:
        results = []
        for hz in _HORIZONS:
            features_hist, labels_hist = self._get_training_data(hz)
            n = len(features_hist)

            if n < _MIN_OUTCOMES_ML:
                out = ArenaOutput(
                    model_name=self.name,
                    version=self.version,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    horizon=hz,
                    spot_at_prediction=spot,
                    prob_up=0.333, prob_down=0.333, prob_range=0.333,
                    confidence=0.0,
                    dominant_scenario="RANGE",
                    data_coverage=0.0,
                    top_factors=[],
                    warnings=[f"Observation — {n}/{_MIN_OUTCOMES_ML} outcomes requis"],
                    features_snapshot=current_features,
                )
            else:
                pb_up, pb_down, pb_range, conf, knn_diag = self._knn_predict(
                    features_hist, labels_hist, current_features
                )
                dominant = _dominant_from_prob3(pb_up, pb_down, pb_range)
                out = ArenaOutput(
                    model_name=self.name,
                    version=self.version,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    horizon=hz,
                    spot_at_prediction=spot,
                    prob_up=pb_up, prob_down=pb_down, prob_range=pb_range,
                    confidence=conf,
                    dominant_scenario=dominant,
                    data_coverage=round(min(1.0, n / 100.0), 3),
                    top_factors=[],
                    warnings=[f"ML k-NN — {n} outcomes historiques"],
                    features_snapshot=current_features,
                    explanation=knn_diag,
                )
            results.append(out)
        return results


# ─────────────────────────── Expert V2 — Crash Gate Engine ──────────────────

class Expert2CrashGateEngine:
    """Expert V1 + corrections V2 : crash regime gate, max pain réduit, dealer priority."""

    name    = _EXPERT2_NAME
    version = _EXPERT2_VERSION

    _BULL_CAP_CRASH      = 0.45   # prob_up plafonné à 45% en crash (vs 55% en V1)
    _MAX_PAIN_MULT_CRASH = 0.30   # max_pain réduit à 30% en regime de stress
    _DEX_MULT_CRASH      = 1.50   # dealer pressure prioritaire en crash (+50%)
    _DEX_MULT_NORMAL     = 1.20   # légère priorité DEX même hors crash

    def predict(
        self,
        spot: float,
        pe_output: ProbabilityEngineOutput,
        features: dict,
    ) -> List[ArenaOutput]:
        regime  = features.get("gex_regime", "NEUTRE")
        dex_dir = features.get("dex_direction", "NEUTRAL")
        is_crash = (regime == "AMPLIFICATEUR")

        pairs = [
            (pe_output.bull_4h,  pe_output.bear_4h,  "4h"),
            (pe_output.bull_24h, pe_output.bear_24h, "24h"),
            (pe_output.bull_72h, pe_output.bear_72h, "72h"),
        ]
        results = []
        for bull, bear, hz in pairs:
            if is_crash:
                mults = {
                    g: (
                        self._MAX_PAIN_MULT_CRASH if g == "max_pain"
                        else self._DEX_MULT_CRASH if g == "dex"
                        else 1.0
                    )
                    for g in _FACTOR_GROUPS
                }
            else:
                mults = {g: (self._DEX_MULT_NORMAL if g == "dex" else 1.0) for g in _FACTOR_GROUPS}

            out = _scenario_pair_to_arena(bull, bear, hz, self.name, self.version, spot, mults)

            # Crash Regime Gate — BULL cap strict
            if is_crash and out.prob_up > self._BULL_CAP_CRASH:
                excess = out.prob_up - self._BULL_CAP_CRASH
                out.prob_up        = round(self._BULL_CAP_CRASH, 3)
                out.prob_down      = round(min(0.90, out.prob_down + excess * 0.70), 3)
                out.prob_range     = round(max(0.0, 1.0 - out.prob_up - out.prob_down), 3)
                out.dominant_scenario = _dominant_from_prob3(out.prob_up, out.prob_down, out.prob_range)
                out.warnings.append(
                    f"V2 Crash Gate actif ({regime}) — BULL plafonné à {int(self._BULL_CAP_CRASH*100)}%"
                )
                if dex_dir == "BEARISH_FLOWS":
                    out.warnings.append("V2: Dealer pressure baissière prioritaire")

            out = _apply_divergence_nudge(out, features)
            results.append(out)
        return results


# ─────────────────────────── Naive Baseline Engine ───────────────────────────

class NaiveBaselineEngine:
    """Baseline naïf — momentum pur sur 24h. Nos moteurs DOIVENT le battre."""

    name    = _NAIVE_NAME
    version = _NAIVE_VERSION

    def predict(self, spot: float) -> List[ArenaOutput]:
        spot_prev = self._get_spot_prev(24)

        if spot_prev and spot_prev > 0:
            ret_pct = (spot - spot_prev) / spot_prev * 100
            if ret_pct > 0.5:
                prob_up, prob_down, prob_range = 0.55, 0.35, 0.10
                direction_lbl = f"momentum haussier +{ret_pct:.2f}%"
            elif ret_pct < -0.5:
                prob_up, prob_down, prob_range = 0.35, 0.55, 0.10
                direction_lbl = f"momentum baissier {ret_pct:.2f}%"
            else:
                prob_up, prob_down, prob_range = 0.40, 0.40, 0.20
                direction_lbl = f"consolidation {ret_pct:+.2f}%"
            fs = {
                "spot": spot,
                "spot_24h_ago": round(spot_prev, 0),
                "momentum_24h_pct": round(ret_pct, 4),
            }
        else:
            prob_up, prob_down, prob_range = 0.40, 0.40, 0.20
            direction_lbl = "pas d'historique — neutre"
            fs = {"spot": spot, "spot_24h_ago": None, "momentum_24h_pct": None}

        dominant = _dominant_from_prob3(prob_up, prob_down, prob_range)

        return [
            ArenaOutput(
                model_name=self.name,
                version=self.version,
                timestamp=datetime.now(timezone.utc).isoformat(),
                horizon=hz,
                spot_at_prediction=spot,
                prob_up=prob_up,
                prob_down=prob_down,
                prob_range=prob_range,
                confidence=0.30,
                dominant_scenario=dominant,
                data_coverage=1.0 if spot_prev else 0.0,
                top_factors=[f"BTC 24h: {direction_lbl}"],
                warnings=["Baseline naïf — référence minimale. Si nos moteurs ne battent pas ça, ils sont mauvais."],
                features_snapshot=fs,
            )
            for hz in _HORIZONS
        ]

    def _get_spot_prev(self, hours: int) -> Optional[float]:
        target_ts = int(time.time()) - hours * 3600
        with _conn() as c:
            row = c.execute(
                """SELECT spot_at_prediction FROM model_predictions
                   WHERE timestamp >= ? AND timestamp <= ? AND spot_at_prediction > 0
                   ORDER BY ABS(timestamp - ?) ASC LIMIT 1""",
                (target_ts - 1800, target_ts + 1800, target_ts),
            ).fetchone()
        if row and row["spot_at_prediction"]:
            return float(row["spot_at_prediction"])
        with _conn() as c:
            row = c.execute(
                """SELECT spot_at_prediction FROM model_predictions
                   WHERE timestamp <= ? AND spot_at_prediction > 0
                   ORDER BY timestamp DESC LIMIT 1""",
                (target_ts,),
            ).fetchone()
        return float(row["spot_at_prediction"]) if row and row["spot_at_prediction"] else None


# ─────────────────────────── MOPI Divergence Engine ──────────────────────────

class MopiDivergenceEngine:
    """Moteur challenger — divergences MOPI vs prix BTC.

    Teste si les divergences MOPI sont plus prédictives que le Naive Baseline.
    Statut : baseline_smart (remplaçant direct du Naive Baseline si meilleur).
    """

    name    = _MDE_NAME
    version = _MDE_VERSION

    def predict(self, spot: float) -> List[ArenaOutput]:
        from .mopi_divergence_engine import (
            _get_history, _select_best_window, _divergence_age,
            build_probabilities, apply_setup_gate, compute_unique_setup_count,
            _MIN_SNAPS, _HORIZONS as _MDE_HORIZONS,
        )

        snapshots = _get_history(hours=26)
        n_snaps   = len(snapshots)
        warnings  = []

        if n_snaps < _MIN_SNAPS:
            prob_up, prob_down, prob_range = 0.25, 0.25, 0.50
            conf, dominant = 0.35, "RANGE"
            div_type, strength, best_feat = "none", 0.0, {}
            warnings.append(f"Données insuffisantes — {n_snaps} snapshots")
        else:
            div_type, strength, best_feat = _select_best_window(snapshots)
            age = _divergence_age(snapshots, div_type)
            prob_up, prob_down, prob_range, conf, dominant = build_probabilities(
                div_type, strength
            )
            best_feat["divergence_age_snapshots"] = age

        # Gate de sécurité : N unique setups (horizon 4h, référence)
        setup_info  = compute_unique_setup_count(_MDE_NAME, "4h", days=30)
        n_unique    = setup_info["unique_setup_count"]
        conf, setup_label, can_promote = apply_setup_gate(conf, n_unique)

        if setup_label == "EXPLORATION":
            warnings.append(
                f"EXPLORATION — {n_unique} setups uniques < 30. "
                "Confidence plafonnée 45%. Ne pas promouvoir."
            )
        elif setup_label == "SIGNAL FRAGILE":
            warnings.append(
                f"SIGNAL FRAGILE — {n_unique} setups uniques (30-99). "
                "Confidence plafonnée 55%."
            )

        explanation = {
            "divergence_type":           div_type,
            "strength":                  strength,
            "window":                    f"{best_feat.get('window', 'n/a')}h",
            "price_slope":               best_feat.get("price_slope"),
            "mopi_slope":                best_feat.get("mopi_slope"),
            "correlation":               best_feat.get("mopi_price_correlation"),
            "mopi_divergence_bullish":   best_feat.get("mopi_divergence_bullish"),
            "mopi_divergence_bearish":   best_feat.get("mopi_divergence_bearish"),
            "price_lower_low":           best_feat.get("price_lower_low"),
            "price_higher_high":         best_feat.get("price_higher_high"),
            "mopi_higher_low":           best_feat.get("mopi_higher_low"),
            "mopi_lower_high":           best_feat.get("mopi_lower_high"),
            "divergence_age_snapshots":  best_feat.get("divergence_age_snapshots", 0),
            "n_snapshots_available":     n_snaps,
            "setup_label":               setup_label,
            "n_unique_setups":           n_unique,
            "can_promote":               can_promote,
            "warnings":                  warnings,
        }

        if div_type == "bullish":
            top_factors = [f"Divergence haussière MOPI (force {strength:.2f}) [{setup_label}]"]
        elif div_type == "bearish":
            top_factors = [f"Divergence baissière MOPI (force {strength:.2f}) [{setup_label}]"]
        else:
            top_factors = [f"Pas de divergence MOPI exploitable [{setup_label}]"]

        coverage = round(min(1.0, n_snaps / 48.0), 3)
        fs = {"mopi_divergence_type": div_type, "strength": strength, "spot": spot,
              "setup_label": setup_label, "n_unique_setups": n_unique}

        return [
            ArenaOutput(
                model_name=self.name,
                version=self.version,
                timestamp=datetime.now(timezone.utc).isoformat(),
                horizon=hz,
                spot_at_prediction=spot,
                prob_up=prob_up,
                prob_down=prob_down,
                prob_range=prob_range,
                confidence=conf,
                dominant_scenario=dominant,
                data_coverage=coverage,
                top_factors=top_factors,
                warnings=warnings,
                features_snapshot=fs,
                explanation=explanation,
            )
            for hz in _HORIZONS
        ]


# ─────────────────────────── Auto-Calibrated Regime Shadow Engine ────────────

class AutoCalibratedRegimeShadowEngine:
    """Shadow challenger: poids globaux Auto-Cal + ajustements par régime (observe mode).

    Teste si l'apprentissage conditionnel au régime améliore les prédictions.
    N'affecte pas le moteur Auto-Calibrated principal — shadow uniquement.

    Logique de poids:
      final_weight[group] = global_autocal_mult[group] × (1 + regime_delta)
      où regime_delta provient des propositions non bloquées de regime_adaptive_weights.
    """

    name    = _ACRS_NAME
    version = _ACRS_VERSION

    # Mapping: engine name (regime_adaptive_weights) → factor_group (model_arena)
    _ENGINE_TO_GROUP: Dict[str, str] = {
        "dealer":   "dex",
        "gex":      "gex",
        "walls":    "walls",
        "max_pain": "max_pain",
        "mopi":     "mopi",
        # "gravity" et "squeeze" = events séparés, pas de rule group correspondant
    }

    def _compute_shadow_weights(
        self, horizon: str
    ) -> Tuple[Dict[str, float], Dict[str, float], List[dict], List[dict], List[str]]:
        """Retourne (global_mults, shadow_mults, applied, blocked, current_regimes)."""
        global_mults = _get_weight_multipliers(_AUTOCAL_NAME, horizon)
        shadow_mults = dict(global_mults)
        applied: List[dict] = []
        blocked: List[dict] = []
        current_regimes: List[str] = []

        try:
            from .regime_adaptive_weights import compute_adaptive_weights
            report = compute_adaptive_weights(days=30)
            current_regimes = report.current_regime

            if not current_regimes:
                return global_mults, shadow_mults, applied, blocked, current_regimes

            for proposal in report.proposals:
                if proposal.regime not in current_regimes:
                    continue
                group = self._ENGINE_TO_GROUP.get(proposal.engine)
                if group is None:
                    continue  # gravity/squeeze: pas de correspondance dans _FACTOR_GROUPS

                global_w = global_mults.get(group, 1.0)
                entry = {
                    "engine":        proposal.engine,
                    "group":         group,
                    "regime":        proposal.regime,
                    "n":             proposal.n,
                    "ev":            proposal.ev,
                    "delta":         proposal.delta,
                    "global_weight": round(global_w, 4),
                    "blocked":       proposal.blocked,
                    "block_reason":  proposal.block_reason,
                }

                if proposal.blocked:
                    entry["final_weight"] = round(global_w, 4)
                    blocked.append(entry)
                else:
                    # poids_final = global × (1 + delta)
                    # base_weight = 1.0 → proposed_weight = 1 + delta
                    # → adj_factor = proposed_weight / base = 1 + delta
                    adj_factor = 1.0 + proposal.delta
                    final_w = round(global_w * adj_factor, 4)
                    shadow_mults[group] = final_w
                    entry["final_weight"] = final_w
                    applied.append(entry)

        except Exception as e:
            log.warning(f"[acrs] regime weights error: {e}")

        return global_mults, shadow_mults, applied, blocked, current_regimes

    def predict(
        self,
        spot: float,
        pe_output: ProbabilityEngineOutput,
        features_snapshot: dict = None,
    ) -> List[ArenaOutput]:
        pairs = [
            (pe_output.bull_4h,  pe_output.bear_4h,  "4h"),
            (pe_output.bull_24h, pe_output.bear_24h, "24h"),
            (pe_output.bull_72h, pe_output.bear_72h, "72h"),
        ]
        results = []
        for bull, bear, hz in pairs:
            global_mults, shadow_mults, applied, blocked, regimes = self._compute_shadow_weights(hz)
            out = _scenario_pair_to_arena(bull, bear, hz, self.name, self.version, spot, shadow_mults)
            # Enrichir l'explication avec les détails shadow (pour debug, jamais pour calibration)
            out.explanation["shadow_regime"]         = regimes
            out.explanation["shadow_n_applied"]      = len(applied)
            out.explanation["shadow_n_blocked"]      = len(blocked)
            out.explanation["shadow_global_weights"] = {k: round(v, 4) for k, v in global_mults.items()}
            out.explanation["shadow_final_weights"]  = {k: round(v, 4) for k, v in shadow_mults.items()}
            out.explanation["shadow_applied"]        = applied
            out.explanation["shadow_blocked"]        = blocked
            if features_snapshot:
                out = _apply_divergence_nudge(out, features_snapshot)
            results.append(out)
        return results


# ─────────────────────────── Adaptive Memory Engine ─────────────────────────

class AdaptiveMemoryEngine:
    """Moteur mémoire adaptative — k-NN pondéré × clusters fiables.

    Étape 1 : voisins historiques les plus proches (12 features)
    Étape 2 : chaque voisin pondéré par 5 facteurs (sim × régime × récence × cluster × qualité)
    Étape 3 : fiabilité des clusters mémorisée dans adaptive_memory_clusters
    Étape 4 : probabilités UP/DOWN/RANGE avec garde-fous
    """

    name    = _AME_NAME
    version = _AME_VERSION

    _N_FEATURES   = 12
    _MIN_NEIGHBORS = 20  # seuil WARMING UP

    def _to_vector(self, f: dict) -> Optional[np.ndarray]:
        try:
            gex_near  = float(f.get("gex_near", 0) or 0)
            v_gex     = float(np.clip(gex_near / 5e9, -1.0, 1.0))

            dex = f.get("dex_direction", "")
            v_dex = 1.0 if "BULLISH" in str(dex) else (-1.0 if "BEARISH" in str(dex) else 0.0)

            iv    = float(f.get("iv_rank", 50) or 50)
            v_iv  = (iv - 50.0) / 50.0

            pcr   = float(f.get("pc_ratio_near", 1.0) or 1.0)
            v_pcr = float(np.clip(pcr - 1.0, -2.0, 2.0)) / 2.0

            mopi  = float(f.get("mopi_score", 50) or 50)
            v_mopi = (mopi - 50.0) / 50.0

            flip_d  = float(f.get("flip_distance_pct", 0) or 0)
            v_flip  = float(np.clip(flip_d, -0.20, 0.20)) / 0.20

            regime  = f.get("gex_regime", "NEUTRE")
            v_regime = 1.0 if regime == "STABILISANT" else (-1.0 if regime == "AMPLIFICATEUR" else 0.0)

            funding = float(f.get("funding_rate", 0) or 0)
            v_fund  = float(np.clip(funding / 0.005, -2.0, 2.0)) / 2.0

            oi  = float(f.get("futures_oi", 0) or 0)
            v_oi = float(np.clip(np.log1p(oi / 1e9) - 2.0, -3.0, 3.0)) / 3.0 if oi > 0 else 0.0

            vol = float(f.get("spot_volume_24h", 0) or 0)
            v_vol = float(np.clip(np.log1p(vol / 1e9) - 3.5, -2.0, 2.0)) / 2.0 if vol > 0 else 0.0

            mp_dist = float(f.get("max_pain_distance_pct", 0) or 0)
            v_mp    = float(np.clip(mp_dist, -0.05, 0.05)) / 0.05

            v_panic = 1.0 if bool(f.get("panic", False)) else 0.0

            div_type = float(f.get("mopi_div_type_enc", 0.0) or 0.0)
            v_div_type = float(np.clip(div_type, -1.0, 1.0))
            div_str  = float(f.get("mopi_div_strength", 0.0) or 0.0)
            v_div_str = float(np.clip(div_str / 0.030, 0.0, 1.0))
            div_corr = float(f.get("mopi_price_corr", 0.0) or 0.0)
            v_div_corr = float(np.clip(div_corr, -1.0, 1.0))

            return np.array([
                v_gex, v_dex, v_iv, v_pcr, v_mopi,
                v_flip, v_regime, v_fund, v_oi, v_vol,
                v_mp, v_panic,
                v_div_type, v_div_str, v_div_corr,
            ], dtype=np.float64)
        except Exception:
            return None

    def _cluster_id(self, f: dict, horizon: str) -> str:
        regime  = f.get("gex_regime", "NEUTRE")
        iv      = float(f.get("iv_rank", 50) or 50)
        iv_bkt  = "L" if iv < 30 else ("H" if iv > 60 else "M")
        dex     = f.get("dex_direction", "NEUTRAL")
        dex_bkt = "B" if "BULLISH" in str(dex) else ("S" if "BEARISH" in str(dex) else "N")
        pnk_bkt = "P" if bool(f.get("panic", False)) else "N"
        return f"{regime}|{iv_bkt}|{dex_bkt}|{pnk_bkt}|{horizon}"

    def _regime_match_score(self, f_hist: dict, f_curr: dict) -> float:
        """Score de similarité de régime [0.2, 1.2]."""
        rh = f_hist.get("gex_regime", "NEUTRE")
        rc = f_curr.get("gex_regime", "NEUTRE")
        ph = bool(f_hist.get("panic", False))
        pc = bool(f_curr.get("panic", False))

        if rh == rc and ph == pc:
            return 1.2
        if ph != pc:
            return 0.2   # forte pénalité Panic vs Normal
        if rh == rc:
            return 1.0
        if {rh, rc} == {"AMPLIFICATEUR", "STABILISANT"}:
            return 0.4   # régimes opposés
        return 0.7

    def _recency_score(self, ts: int, now: int) -> float:
        """Léger bonus récence, décroissance linéaire sur 30j → [0.8, 1.1]."""
        age_days = (now - ts) / 86400.0
        return max(0.8, 1.1 - age_days * (0.3 / 30.0))

    def _data_quality_score(self, f: dict) -> float:
        """Complétude des features [0.5, 1.0]."""
        required = ["gex_near", "dex_direction", "iv_rank", "mopi_score", "gex_regime"]
        optional = ["pc_ratio_near", "flip_distance_pct", "funding_rate", "futures_oi", "spot_volume_24h"]
        rq = sum(1 for k in required if f.get(k) is not None) / len(required)
        op = sum(1 for k in optional if f.get(k) is not None) / len(optional)
        return 0.5 + 0.35 * rq + 0.15 * op

    def _get_cluster_reliability(self, cluster_id: str, horizon: str) -> Tuple[float, int]:
        """Fiabilité historique du cluster depuis adaptive_memory_clusters.

        Retourne (reliability_score [0.5, 1.5], n_outcomes).
        Si N < 5 outcomes → neutre (1.0, 0).
        """
        with _conn() as c:
            row = c.execute(
                "SELECT n_outcomes, reliability_score "
                "FROM adaptive_memory_clusters WHERE cluster_id=? AND horizon=?",
                (cluster_id, horizon),
            ).fetchone()
        if row and row["n_outcomes"] and row["n_outcomes"] >= 5:
            return float(row["reliability_score"] or 1.0), int(row["n_outcomes"])
        return 1.0, 0

    def _get_training_data(
        self, horizon: str
    ) -> Tuple[List[dict], List[str], List[int]]:
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.features_json, mp.timestamp, mo.realized_direction
                   FROM model_predictions mp
                   JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = ? AND mp.horizon = ?
                     AND mo.realized_direction IS NOT NULL
                     AND mp.is_seed = 0
                   ORDER BY mp.timestamp DESC LIMIT 500""",
                (_EXPERT_NAME, horizon),
            ).fetchall()
        features, labels, timestamps = [], [], []
        for r in rows:
            try:
                feat = json.loads(r["features_json"] or "{}")
                if feat:
                    features.append(feat)
                    labels.append(r["realized_direction"])
                    timestamps.append(r["timestamp"])
            except Exception:
                pass
        return features, labels, timestamps

    def _weighted_knn_predict(
        self,
        features_hist: List[dict],
        labels_hist: List[str],
        timestamps_hist: List[int],
        current_features: dict,
        horizon: str,
    ) -> Tuple[float, float, float, float, dict]:
        _empty: dict = {
            "n_neighbors_used": 0,
            "n_outcomes_available": 0,
            "avg_distance": None,
            "regime_match_pct": None,
            "cluster_id": self._cluster_id(current_features, horizon),
            "ame_cluster_id": self._cluster_id(current_features, horizon),
            "cluster_reliability": None,
            "neighbor_distribution": {"UP": 0, "DOWN": 0, "RANGE": 0},
            "status": "WARMING UP",
            "warnings": [],
        }

        if len(features_hist) < self._MIN_NEIGHBORS:
            return 0.333, 0.333, 0.333, 0.0, _empty

        curr_vec = self._to_vector(current_features)
        if curr_vec is None:
            return 0.333, 0.333, 0.333, 0.0, _empty

        now_ts = int(time.time())

        hist_vecs, hist_labels, hist_ts, hist_feats = [], [], [], []
        for f, l, ts in zip(features_hist, labels_hist, timestamps_hist):
            if self._data_quality_score(f) < 0.5:
                continue
            v = self._to_vector(f)
            if v is not None:
                hist_vecs.append(v)
                hist_labels.append(l)
                hist_ts.append(ts)
                hist_feats.append(f)

        n_avail = len(hist_vecs)
        if n_avail < self._MIN_NEIGHBORS:
            return 0.333, 0.333, 0.333, 0.0, _empty

        X = np.array(hist_vecs)
        norms_x = np.linalg.norm(X, axis=1, keepdims=True)
        norms_x = np.where(norms_x < 1e-10, 1e-10, norms_x)
        X_n = X / norms_x

        norm_c = np.linalg.norm(curr_vec)
        if norm_c < 1e-10:
            return 0.333, 0.333, 0.333, 0.0, _empty
        curr_n = curr_vec / norm_c

        sims = X_n @ curr_n
        k = max(10, min(30, n_avail // 5))
        top_idx = np.argsort(sims)[-k:]

        cluster_id           = self._cluster_id(current_features, horizon)
        cluster_rel, n_clust = self._get_cluster_reliability(cluster_id, horizon)
        curr_quality         = self._data_quality_score(current_features)

        up_w = down_w = range_w = 0.0
        n_regime_match = 0
        total_dist = 0.0

        for idx in top_idx:
            sim_score = float(max(0.0, sims[idx]))
            f_hist = hist_feats[idx]
            ts_hist = hist_ts[idx]
            label   = hist_labels[idx]

            regime_score  = self._regime_match_score(f_hist, current_features)
            recency_score = self._recency_score(ts_hist, now_ts)
            hist_quality  = self._data_quality_score(f_hist)

            weight = (
                sim_score
                * regime_score
                * recency_score
                * cluster_rel
                * hist_quality
            )

            if f_hist.get("gex_regime") == current_features.get("gex_regime"):
                n_regime_match += 1

            total_dist += float(1.0 - np.clip(sims[idx], -1.0, 1.0))

            if label == "UP":
                up_w += weight
            elif label == "DOWN":
                down_w += weight
            else:
                range_w += weight

        # Laplace smoothing
        _LAPLACE = 0.5
        up_w    += _LAPLACE
        down_w  += _LAPLACE
        range_w += _LAPLACE
        total_w  = up_w + down_w + range_w

        prob_up    = round(float(up_w    / total_w), 3)
        prob_down  = round(float(down_w  / total_w), 3)
        prob_range = round(max(0.0, 1.0 - prob_up - prob_down), 3)

        conf = float(np.mean(np.clip(sims[top_idx], 0.0, 1.0)))

        # ── Garde-fous ────────────────────────────────────────────────────────
        warnings_ame: List[str] = []

        if k < 20:
            conf = min(conf, 0.60)
            warnings_ame.append(f"N voisins {k}<20 — confiance cap 60%")

        if n_clust < 30:
            conf = min(conf, 0.60)
            warnings_ame.append(f"Cluster N={n_clust}<30 — confiance cap 60%")

        regime_match_pct = n_regime_match / k if k > 0 else 0.0
        if regime_match_pct < 0.50:
            conf = min(conf, 0.55)
            warnings_ame.append(f"Régime mismatch {regime_match_pct:.0%} — cap 55%")

        if curr_quality < 0.70:
            conf = min(conf, 0.50)
            warnings_ame.append(f"Qualité données {curr_quality:.0%} — cap 50%")

        avg_dist   = round(total_dist / k, 4) if k > 0 else None
        top_labels = [hist_labels[i] for i in top_idx]

        diag: dict = {
            "n_neighbors_used":     k,
            "n_outcomes_available": n_avail,
            "avg_distance":         avg_dist,
            "regime_match_pct":     round(regime_match_pct, 3),
            "cluster_id":           cluster_id,
            "ame_cluster_id":       cluster_id,
            "cluster_reliability":  round(cluster_rel, 4),
            "cluster_n_outcomes":   n_clust,
            "neighbor_distribution": {
                "UP":    top_labels.count("UP"),
                "DOWN":  top_labels.count("DOWN"),
                "RANGE": top_labels.count("RANGE"),
            },
            "status":   "ACTIVE",
            "warnings": warnings_ame,
        }

        # Justification obligatoire si prob > 90%
        max_prob = max(prob_up, prob_down, prob_range)
        if max_prob > 0.90:
            diag["justification_high_confidence"] = {
                "k_used":             k,
                "avg_distance":       avg_dist,
                "regime_match_pct":   round(regime_match_pct, 3),
                "cluster_reliability": round(cluster_rel, 4),
                "ev_cluster":         None,  # populated by refresh_ame_clusters
            }

        return prob_up, prob_down, prob_range, round(conf, 3), diag

    def predict(self, spot: float, current_features: dict) -> List[ArenaOutput]:
        results = []
        for hz in _HORIZONS:
            features_hist, labels_hist, timestamps_hist = self._get_training_data(hz)
            n = len(features_hist)
            cluster_id = self._cluster_id(current_features, hz)

            if n < self._MIN_NEIGHBORS:
                out = ArenaOutput(
                    model_name=self.name,
                    version=self.version,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    horizon=hz,
                    spot_at_prediction=spot,
                    prob_up=0.333, prob_down=0.333, prob_range=0.333,
                    confidence=0.0,
                    dominant_scenario="RANGE",
                    data_coverage=0.0,
                    top_factors=[],
                    warnings=[f"WARMING UP — {n}/{self._MIN_NEIGHBORS} voisins min requis"],
                    features_snapshot=current_features,
                    explanation={
                        "status":               "WARMING UP",
                        "n_outcomes_available": n,
                        "ame_cluster_id":       cluster_id,
                    },
                )
            else:
                pb_up, pb_down, pb_range, conf, diag = self._weighted_knn_predict(
                    features_hist, labels_hist, timestamps_hist, current_features, hz
                )
                dominant = _dominant_from_prob3(pb_up, pb_down, pb_range)
                warnings = [f"Mémoire adaptative — {n} setups historiques"]
                warnings.extend(diag.get("warnings", []))

                out = ArenaOutput(
                    model_name=self.name,
                    version=self.version,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    horizon=hz,
                    spot_at_prediction=spot,
                    prob_up=pb_up, prob_down=pb_down, prob_range=pb_range,
                    confidence=conf,
                    dominant_scenario=dominant,
                    data_coverage=round(min(1.0, n / 200.0), 3),
                    top_factors=[],
                    warnings=warnings,
                    features_snapshot=current_features,
                    explanation=diag,
                )
            results.append(out)
        return results


# ─────────────────────────── Neural Utilities ────────────────────────────────

def _sig(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


def _softmax_n(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / (e.sum(axis=-1, keepdims=True) + 1e-12)


def _relu_n(x: np.ndarray) -> np.ndarray:
    return np.maximum(0.0, x)


_LABEL_IDX: Dict[str, int] = {"UP": 0, "DOWN": 1, "RANGE": 2}
_IDX_LABEL: Dict[int, str] = {0: "UP", 1: "DOWN", 2: "RANGE"}


class _AdamState:
    """Adam optimizer state for a parameter list."""

    def __init__(self, shapes: list):
        self._m = [np.zeros(s) for s in shapes]
        self._v = [np.zeros(s) for s in shapes]
        self._t = 0

    def step(
        self,
        params: list,
        grads: list,
        lr: float = 1e-3,
        b1: float = 0.9,
        b2: float = 0.999,
        eps: float = 1e-8,
    ) -> list:
        self._t += 1
        out = []
        for i, (p, g) in enumerate(zip(params, grads)):
            self._m[i] = b1 * self._m[i] + (1 - b1) * g
            self._v[i] = b2 * self._v[i] + (1 - b2) * g ** 2
            m_hat = self._m[i] / (1 - b1 ** self._t)
            v_hat = self._v[i] / (1 - b2 ** self._t)
            out.append(p - lr * m_hat / (np.sqrt(v_hat) + eps))
        return out


# ─────────────────────────── MLP Model ───────────────────────────────────────

class _MLPModel:
    """MLP léger : D → 32 → 16 → 3 (softmax). Adam + early stopping."""

    def __init__(self, input_dim: int, seed: int = 42):
        rng = np.random.default_rng(seed)
        D = input_dim
        self.W1 = rng.normal(0, np.sqrt(2.0 / D),  (D,  32)).astype(np.float64)
        self.b1 = np.zeros(32)
        self.W2 = rng.normal(0, np.sqrt(2.0 / 32), (32, 16)).astype(np.float64)
        self.b2 = np.zeros(16)
        self.W3 = rng.normal(0, np.sqrt(2.0 / 16), (16,  3)).astype(np.float64)
        self.b3 = np.zeros(3)
        self._trained = False

    def _params(self) -> list:
        return [self.W1, self.b1, self.W2, self.b2, self.W3, self.b3]

    def _set(self, p: list):
        self.W1, self.b1, self.W2, self.b2, self.W3, self.b3 = p

    def _fwd(self, X: np.ndarray):
        h1 = _relu_n(X @ self.W1 + self.b1)
        h2 = _relu_n(h1 @ self.W2 + self.b2)
        probs = _softmax_n(h2 @ self.W3 + self.b3)
        return probs, h1, h2

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        p, _, _ = self._fwd(X)
        return p

    def _loss_grads(self, X: np.ndarray, y: np.ndarray, l2: float = 1e-3,
                    class_weights: Optional[np.ndarray] = None):
        n = len(X)
        probs, h1, h2 = self._fwd(X)

        # Sample weights — corrige le class imbalance UP/DOWN/RANGE
        if class_weights is not None:
            sw = class_weights[y]
            sw = sw / (sw.mean() + 1e-10)
        else:
            sw = np.ones(n)

        loss = -(np.log(np.clip(probs[np.arange(n), y], 1e-10, 1.0)) * sw).mean()
        loss += l2 / 2 * (np.sum(self.W1 ** 2) + np.sum(self.W2 ** 2) + np.sum(self.W3 ** 2))

        dy = probs.copy()
        dy[np.arange(n), y] -= 1.0
        dy *= (sw[:, None] / n)

        dW3 = h2.T @ dy + l2 * self.W3
        db3 = dy.sum(0)
        dh2 = dy @ self.W3.T * (h2 > 0)
        dW2 = h1.T @ dh2 + l2 * self.W2
        db2 = dh2.sum(0)
        dh1 = dh2 @ self.W2.T * (h1 > 0)
        dW1 = X.T @ dh1 + l2 * self.W1
        db1 = dh1.sum(0)
        return loss, [dW1, db1, dW2, db2, dW3, db3]

    def train(
        self,
        X_tr: np.ndarray, y_tr: np.ndarray,
        X_va: np.ndarray, y_va: np.ndarray,
        epochs: int = 150, batch: int = 32,
        lr: float = 5e-4, patience: int = 12, l2: float = 1e-3,
        class_weights: Optional[np.ndarray] = None,
    ) -> dict:
        n = len(X_tr)
        adam = _AdamState([p.shape for p in self._params()])
        best_loss = float("inf")
        best_p = [p.copy() for p in self._params()]
        no_imp = 0
        metrics: dict = {}
        rng = np.random.default_rng(7)

        for _ in range(epochs):
            idx = rng.permutation(n)
            for s in range(0, n, batch):
                Xb, yb = X_tr[idx[s: s + batch]], y_tr[idx[s: s + batch]]
                _, grads = self._loss_grads(Xb, yb, l2=l2, class_weights=class_weights)
                self._set(adam.step(self._params(), grads, lr=lr))

            vp, _, _ = self._fwd(X_va)
            vl = -np.log(np.clip(vp[np.arange(len(y_va)), y_va], 1e-10, 1.0)).mean()
            if vl < best_loss - 1e-5:
                best_loss = vl
                best_p = [p.copy() for p in self._params()]
                no_imp = 0
                metrics = self._metrics(X_va, y_va)
            else:
                no_imp += 1
                if no_imp >= patience:
                    break

        self._set(best_p)
        self._trained = True
        return metrics

    def _metrics(self, X: np.ndarray, y: np.ndarray) -> dict:
        preds = self.predict_proba(X).argmax(1)
        cm = np.zeros((3, 3), dtype=int)
        for t, p in zip(y, preds):
            cm[t, p] += 1
        return {"val_winrate": round(float((preds == y).mean()), 4),
                "val_n": len(y),
                "confusion_matrix": cm.tolist()}

    def feature_importance(self, X: np.ndarray, y: np.ndarray) -> List[dict]:
        base = (self.predict_proba(X).argmax(1) == y).mean()
        scores = []
        rng = np.random.default_rng(99)
        for i in range(X.shape[1]):
            Xp = X.copy(); Xp[:, i] = rng.permutation(Xp[:, i])
            scores.append(round(float(base - (self.predict_proba(Xp).argmax(1) == y).mean()), 4))
        return scores

    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, W1=self.W1, b1=self.b1, W2=self.W2, b2=self.b2, W3=self.W3, b3=self.b3)

    def load(self, path: str) -> bool:
        p = Path(path + ".npz")
        if not p.exists():
            return False
        try:
            d = np.load(str(p))
            self.W1, self.b1 = d["W1"], d["b1"]
            self.W2, self.b2 = d["W2"], d["b2"]
            self.W3, self.b3 = d["W3"], d["b3"]
            self._trained = True
            return True
        except Exception:
            return False


# ─────────────────────────── GRU Model ───────────────────────────────────────

class _GRUModel:
    """GRU léger (T, D) → H → 3 (softmax). BPTT analytique + Adam."""

    def __init__(self, input_dim: int, hidden_dim: int = 32, seed: int = 42):
        rng = np.random.default_rng(seed)
        D, H = input_dim, hidden_dim
        sx, sh = np.sqrt(2.0 / D), np.sqrt(1.0 / H)

        self.Wz = rng.normal(0, sx, (D, H)); self.Uz = rng.normal(0, sh, (H, H)); self.bz = np.zeros(H)
        self.Wr = rng.normal(0, sx, (D, H)); self.Ur = rng.normal(0, sh, (H, H)); self.br = np.zeros(H)
        self.Wh = rng.normal(0, sx, (D, H)); self.Uh = rng.normal(0, sh, (H, H)); self.bh = np.zeros(H)
        self.Wy = rng.normal(0, np.sqrt(2.0 / H), (H, 3)); self.by = np.zeros(3)
        self._H = H
        self._trained = False

    def _params(self) -> list:
        return [self.Wz, self.Uz, self.bz, self.Wr, self.Ur, self.br,
                self.Wh, self.Uh, self.bh, self.Wy, self.by]

    def _set(self, p: list):
        (self.Wz, self.Uz, self.bz, self.Wr, self.Ur, self.br,
         self.Wh, self.Uh, self.bh, self.Wy, self.by) = p

    def _gru_step(self, x: np.ndarray, h: np.ndarray):
        z = _sig(x @ self.Wz + h @ self.Uz + self.bz)
        r = _sig(x @ self.Wr + h @ self.Ur + self.br)
        c = np.tanh(x @ self.Wh + (r * h) @ self.Uh + self.bh)
        return (1 - z) * h + z * c, z, r, c

    def _forward(self, X: np.ndarray):
        T = X.shape[0]
        H = self._H
        hs = np.zeros((T + 1, H))
        zs, rs, cs = np.zeros((T, H)), np.zeros((T, H)), np.zeros((T, H))
        for t in range(T):
            hs[t + 1], zs[t], rs[t], cs[t] = self._gru_step(X[t], hs[t])
        logits = hs[-1] @ self.Wy + self.by
        return _softmax_n(logits), hs, zs, rs, cs

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        probs, _, _, _, _ = self._forward(X)
        return probs

    def predict_batch(self, X_batch: np.ndarray) -> np.ndarray:
        return np.array([self.predict_proba(X_batch[i]) for i in range(len(X_batch))])

    def _bptt(self, X: np.ndarray, y_idx: int, sample_weight: float = 1.0):
        probs, hs, zs, rs, cs = self._forward(X)
        T = X.shape[0]

        loss = -np.log(np.clip(probs[y_idx], 1e-10, 1.0)) * sample_weight
        dy = probs.copy(); dy[y_idx] -= 1.0
        dy *= sample_weight

        dWy = np.outer(hs[-1], dy); dby = dy.copy()
        dh = dy @ self.Wy.T

        dWz = np.zeros_like(self.Wz); dUz = np.zeros_like(self.Uz); dbz = np.zeros(self._H)
        dWr = np.zeros_like(self.Wr); dUr = np.zeros_like(self.Ur); dbr = np.zeros(self._H)
        dWh = np.zeros_like(self.Wh); dUh = np.zeros_like(self.Uh); dbh = np.zeros(self._H)

        for t in reversed(range(T)):
            x, h, z, r, c = X[t], hs[t], zs[t], rs[t], cs[t]
            dc = dh * z
            du = dc * (1.0 - c ** 2)
            dz = dh * (c - h)
            dv = dz * z * (1.0 - z)
            dr = (du @ self.Uh.T) * h
            ds = dr * r * (1.0 - r)

            dWh += np.outer(x, du); dUh += np.outer(r * h, du); dbh += du
            dWz += np.outer(x, dv); dUz += np.outer(h, dv);     dbz += dv
            dWr += np.outer(x, ds); dUr += np.outer(h, ds);     dbr += ds

            dh = (dh * (1.0 - z) + dv @ self.Uz.T + ds @ self.Ur.T + (du @ self.Uh.T) * r)

        return loss, [dWz, dUz, dbz, dWr, dUr, dbr, dWh, dUh, dbh, dWy, dby]

    def train(
        self,
        X_tr: np.ndarray, y_tr: np.ndarray,
        X_va: np.ndarray, y_va: np.ndarray,
        epochs: int = 25, batch: int = 16,
        lr: float = 1e-3, patience: int = 8, max_norm: float = 5.0,
        class_weights: Optional[np.ndarray] = None,
    ) -> dict:
        n = len(X_tr)
        adam = _AdamState([p.shape for p in self._params()])
        best_loss = float("inf")
        best_p = [p.copy() for p in self._params()]
        no_imp = 0
        metrics: dict = {}
        rng = np.random.default_rng(7)

        for _ in range(epochs):
            idx = rng.permutation(n)
            for s in range(0, n, batch):
                b_idx = idx[s: s + batch]
                ag = [np.zeros(p.shape) for p in self._params()]
                for i in b_idx:
                    sw = float(class_weights[y_tr[i]]) if class_weights is not None else 1.0
                    _, gi = self._bptt(X_tr[i], y_tr[i], sample_weight=sw)
                    for j, g in enumerate(gi):
                        ag[j] += g
                b = len(b_idx)
                ag = [g / b for g in ag]
                norm = np.sqrt(sum(np.sum(g ** 2) for g in ag))
                if norm > max_norm:
                    scale = max_norm / (norm + 1e-8)
                    ag = [g * scale for g in ag]
                self._set(adam.step(self._params(), ag, lr=lr))

            vp = self.predict_batch(X_va)
            vl = -np.log(np.clip(vp[np.arange(len(y_va)), y_va], 1e-10, 1.0)).mean()
            if vl < best_loss - 1e-5:
                best_loss = vl
                best_p = [p.copy() for p in self._params()]
                no_imp = 0
                metrics = self._metrics(X_va, y_va)
            else:
                no_imp += 1
                if no_imp >= patience:
                    break

        self._set(best_p)
        self._trained = True
        return metrics

    def _metrics(self, X: np.ndarray, y: np.ndarray) -> dict:
        preds = self.predict_batch(X).argmax(1)
        cm = np.zeros((3, 3), dtype=int)
        for t, p in zip(y, preds):
            cm[t, p] += 1
        return {"val_winrate": round(float((preds == y).mean()), 4),
                "val_n": len(y),
                "confusion_matrix": cm.tolist()}

    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, Wz=self.Wz, Uz=self.Uz, bz=self.bz, Wr=self.Wr, Ur=self.Ur, br=self.br,
                 Wh=self.Wh, Uh=self.Uh, bh=self.bh, Wy=self.Wy, by=self.by)

    def load(self, path: str) -> bool:
        p = Path(path + ".npz")
        if not p.exists():
            return False
        try:
            d = np.load(str(p))
            self.Wz, self.Uz, self.bz = d["Wz"], d["Uz"], d["bz"]
            self.Wr, self.Ur, self.br = d["Wr"], d["Ur"], d["br"]
            self.Wh, self.Uh, self.bh = d["Wh"], d["Uh"], d["bh"]
            self.Wy, self.by = d["Wy"], d["by"]
            self._trained = True
            return True
        except Exception:
            return False


# ─────────────────────────── Neural Tabular Engine ───────────────────────────

class NeuralTabularEngine:
    """MLP sur les features options instantanées — T=0, pas de séquence.

    Garde-fous :
      WARMING_UP    : N < 300 outcomes
      SHADOW        : 300 ≤ N < 1000, confidence ≤ 60%
      OBSERVATION   : N ≥ 1000, confidence ≤ 75%
      Jamais > 85%. Pas de promotion automatique.
    """

    name    = _NEURAL_TAB_NAME
    version = _NEURAL_TAB_VERSION

    _FEAT_NAMES = [
        "gex_near", "dex", "iv_rank", "pcr_near", "mopi_score",
        "flip_dist", "regime_stab", "regime_amp",
        "funding", "futures_oi", "spot_volume", "mp_dte",
        "put_wall_flag", "call_wall_flag",
        "mopi_div_type", "mopi_div_strength", "mopi_price_corr",
    ]

    def __init__(self):
        self._models: Dict[str, _MLPModel] = {}
        self._meta:   Dict[str, dict]       = {}
        self._retrain_attempts:     Dict[str, int]   = {hz: 0 for hz in _HORIZONS}
        self._last_retrain_attempt: Dict[str, float] = {hz: 0.0 for hz in _HORIZONS}
        self._last_prediction_ts:   Dict[str, float] = {hz: 0.0 for hz in _HORIZONS}
        self._load_all()

    def _mpath(self, hz: str) -> str:
        return os.path.join(_NEURAL_MODEL_DIR, f"tabular_{hz}")

    def _metapath(self, hz: str) -> str:
        return os.path.join(_NEURAL_MODEL_DIR, f"tabular_{hz}_meta.json")

    def _load_all(self):
        for hz in _HORIZONS:
            m = _MLPModel(_NEURAL_TAB_FEATURES)
            if m.load(self._mpath(hz)):
                # Invalider le modèle si la dimension d'entrée a changé
                if m.W1.shape[0] != _NEURAL_TAB_FEATURES:
                    log.warning(
                        f"[neural_tab] {hz} — dimension W1 {m.W1.shape[0]} ≠ "
                        f"{_NEURAL_TAB_FEATURES}, modèle invalidé (retraining)"
                    )
                else:
                    self._models[hz] = m
            mp = Path(self._metapath(hz))
            if mp.exists():
                try:
                    with open(mp) as f:
                        self._meta[hz] = json.load(f)
                except Exception:
                    pass

    def _save(self, hz: str, model: _MLPModel, meta: dict):
        Path(_NEURAL_MODEL_DIR).mkdir(parents=True, exist_ok=True)
        model.save(self._mpath(hz))
        with open(self._metapath(hz), "w") as f:
            json.dump(meta, f, ensure_ascii=False)
        self._models[hz] = model
        self._meta[hz] = meta

    def _vec(self, f: dict) -> Optional[np.ndarray]:
        try:
            gex  = float(f.get("gex_near", 0) or 0)
            dex  = f.get("dex_direction", "")
            iv   = float(f.get("iv_rank", 50) or 50)
            pcr  = float(f.get("pc_ratio_near", 1.0) or 1.0)
            mopi = float(f.get("mopi_score", 50) or 50)
            flip = float(f.get("flip_distance_pct", 0) or 0)
            reg  = str(f.get("gex_regime", "NEUTRE"))
            fund = float(f.get("funding_rate", 0) or 0)
            oi   = float(f.get("futures_oi", 0) or 0)
            vol  = float(f.get("spot_volume_24h", 0) or 0)
            dte  = float(f.get("max_pain_dte", 15) or 15)
            div_type = float(f.get("mopi_div_type_enc", 0.0) or 0.0)
            div_str  = float(f.get("mopi_div_strength", 0.0) or 0.0)
            div_corr = float(f.get("mopi_price_corr", 0.0) or 0.0)

            return np.array([
                float(np.clip(gex / 5e9, -1.0, 1.0)),
                1.0 if "BULLISH" in str(dex) else (-1.0 if "BEARISH" in str(dex) else 0.0),
                (iv - 50.0) / 50.0,
                float(np.clip((pcr - 1.0) / 2.0, -1.0, 1.0)),
                (mopi - 50.0) / 50.0,
                float(np.clip(flip / 0.20, -1.0, 1.0)),
                1.0 if reg == "STABILISANT"   else 0.0,
                1.0 if reg == "AMPLIFICATEUR" else 0.0,
                float(np.clip(fund / 0.005, -2.0, 2.0)) / 2.0,
                float(np.clip(np.log1p(oi  / 1e9) / 5.0, -1.0, 1.0)) if oi  > 0 else 0.0,
                float(np.clip(np.log1p(vol / 1e9) / 5.0, -1.0, 1.0)) if vol > 0 else 0.0,
                float(np.clip(dte / 30.0, 0.0, 2.0)),
                1.0 if f.get("put_wall")  else 0.0,
                1.0 if f.get("call_wall") else 0.0,
                float(np.clip(div_type, -1.0, 1.0)),
                float(np.clip(div_str / 0.030, 0.0, 1.0)),
                float(np.clip(div_corr, -1.0, 1.0)),
            ], dtype=np.float64)
        except Exception:
            return None

    def _training_data(self, hz: str):
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.features_json, mo.realized_direction, mo.return_pct
                   FROM model_predictions mp
                   JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = ? AND mp.horizon = ?
                     AND mo.realized_direction IS NOT NULL AND mp.is_seed = 0
                   ORDER BY mp.timestamp ASC LIMIT 2000""",
                (_EXPERT_NAME, hz),
            ).fetchall()
        feats, labels, returns = [], [], []
        for r in rows:
            try:
                f = json.loads(r["features_json"] or "{}")
                if f:
                    feats.append(f)
                    labels.append(r["realized_direction"])
                    returns.append(float(r["return_pct"]) if r["return_pct"] is not None else 0.0)
            except Exception:
                pass
        return feats, labels, returns

    def _stale(self, hz: str) -> bool:
        meta = self._meta.get(hz, {})
        if not self._models.get(hz, _MLPModel(_NEURAL_TAB_FEATURES))._trained:
            return True
        return (time.time() - meta.get("trained_at", 0)) > _NEURAL_RETRAIN_H * 3600

    def _retrain(self, hz: str):
        t_start = time.time()
        self._last_retrain_attempt[hz] = t_start
        self._retrain_attempts[hz] = self._retrain_attempts.get(hz, 0) + 1
        try:
            feats, labels, returns = self._training_data(hz)
            n = len(feats)
            if n < _NEURAL_TAB_WARMUP:
                _log_retrain_attempt(_NEURAL_TAB_NAME, hz, "skipped_no_data", n, time.time() - t_start)
                return

            vecs, ys, rets = [], [], []
            for f, l, r in zip(feats, labels, returns):
                v   = self._vec(f)
                idx = _LABEL_IDX.get(l)
                if v is not None and idx is not None:
                    vecs.append(v); ys.append(idx); rets.append(r)
            if len(vecs) < _NEURAL_TAB_WARMUP:
                _log_retrain_attempt(_NEURAL_TAB_NAME, hz, "skipped_no_data", len(vecs), time.time() - t_start)
                return

            X     = np.array(vecs, dtype=np.float64)
            y     = np.array(ys,   dtype=int)
            r_arr = np.array(rets, dtype=np.float64)
            split = int(len(X) * 0.70)
            if split < 50 or (len(X) - split) < 30:
                _log_retrain_attempt(_NEURAL_TAB_NAME, hz, "skipped_split_too_small", len(vecs), time.time() - t_start)
                return

            Xtr, ytr = X[:split], y[:split]
            Xva, yva = X[split:], y[split:]
            rva      = r_arr[split:]
            mu       = Xtr.mean(0); std = Xtr.std(0) + 1e-8
            Xtr_n    = (Xtr - mu) / std; Xva_n = (Xva - mu) / std

            # Class weights pour corriger le déséquilibre UP/DOWN/RANGE
            cw = _class_weights_from_labels(ytr)
            model   = _MLPModel(_NEURAL_TAB_FEATURES, seed=int(time.time()) % 10000)
            metrics = model.train(Xtr_n, ytr, Xva_n, yva, class_weights=cw)
            val_wr  = metrics.get("val_winrate", 0.0)

            # Compute val EV and PF using neural model's predictions on val set
            val_probs    = model.predict_proba(Xva_n)
            val_pred_idx = np.argmax(val_probs, axis=1)
            dir_adj_rets = [
                float(ret) if _IDX_LABEL[int(pi)] == "UP"
                else (-float(ret) if _IDX_LABEL[int(pi)] == "DOWN" else 0.0)
                for pi, ret in zip(val_pred_idx, rva)
            ]
            val_ev  = round(float(np.mean(dir_adj_rets)), 4) if dir_adj_rets else None
            wins    = [r for r in dir_adj_rets if r > 0]
            losses  = [r for r in dir_adj_rets if r < 0]
            val_pf  = round(sum(wins) / abs(sum(losses)), 3) if losses else None

            fi_raw    = model.feature_importance(Xva_n, yva)
            fi_list   = [{"feature": nm, "importance": sc}
                         for nm, sc in sorted(zip(self._FEAT_NAMES, fi_raw), key=lambda x: -x[1])]
            top_feats = [fi["feature"] for fi in fi_list[:5]]
            duration_s = round(time.time() - t_start, 2)

            meta = {
                "trained_at":         int(time.time()),
                "n_training_samples": len(vecs),
                "train_period":       {"n_train": split, "n_val": len(Xva)},
                "val_winrate":        val_wr,
                "val_ev":             val_ev,
                "val_pf":             val_pf,
                "val_n":              metrics.get("val_n", 0),
                "confusion_matrix":   metrics.get("confusion_matrix", []),
                "top_features":       top_feats,
                "fi_scores":          fi_list,
                "model_status":       "OBSERVATION" if val_wr >= 0.45 else "INVALID",
                "scaler":             {"mean": mu.tolist(), "std": std.tolist()},
            }
            self._save(hz, model, meta)

            with _conn() as c:
                c.execute(
                    """INSERT INTO neural_training_log
                       (model_name, horizon, trained_at, n_samples, val_winrate, val_ev, val_pf, duration_s, status, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (_NEURAL_TAB_NAME, hz, int(time.time()), len(vecs), val_wr, val_ev, val_pf, duration_s, "success", "auto"),
                )
                c.commit()
            _log_retrain_attempt(_NEURAL_TAB_NAME, hz, "success", len(vecs), duration_s)

        except Exception as e:
            _log_retrain_attempt(_NEURAL_TAB_NAME, hz, "failed", 0, time.time() - t_start, str(e)[:500])
            raise

    def predict(self, spot: float, features_snapshot: dict) -> List[ArenaOutput]:
        return [self._predict_hz(spot, features_snapshot, hz) for hz in _HORIZONS]

    def _predict_hz(self, spot: float, f: dict, hz: str) -> ArenaOutput:
        ts   = datetime.now(timezone.utc).isoformat()
        warn: List[str] = []
        self._last_prediction_ts[hz] = time.time()

        if self._stale(hz):
            try:
                self._retrain(hz)
            except Exception as e:
                log.warning(f"[neural_tabular] retrain {hz}: {e}")

        meta = self._meta.get(hz, {})
        n    = meta.get("n_training_samples", 0)

        if n < _NEURAL_TAB_WARMUP:
            warn.append(f"WARMING_UP — {n}/{_NEURAL_TAB_WARMUP} outcomes requis")
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE",
                data_coverage=round(n / _NEURAL_TAB_WARMUP, 3),
                top_factors=[], warnings=warn, features_snapshot=f,
                explanation={"model_status": "WARMING_UP", "n_training_samples": n},
            )

        if meta.get("model_status") == "INVALID":
            warn.append("WARMING_UP — val_winrate < 0.45, modèle non publié")
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=warn, features_snapshot=f,
                explanation={"model_status": "INVALID"},
            )

        model = self._models.get(hz)
        if model is None or not model._trained:
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=["NOT_READY"], features_snapshot=f,
                explanation={"model_status": "NOT_READY"},
            )

        vec = self._vec(f)
        if vec is None:
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=["FEATURE_ERROR"], features_snapshot=f,
                explanation={"model_status": "FEATURE_ERROR"},
            )

        sc  = meta.get("scaler", {})
        mu  = np.array(sc.get("mean", np.zeros(_NEURAL_TAB_FEATURES)))
        std = np.array(sc.get("std",  np.ones(_NEURAL_TAB_FEATURES)))
        vec_n = (vec - mu) / (std + 1e-8)

        probs = model.predict_proba(vec_n.reshape(1, -1))[0]
        pu, pd, pr = float(probs[0]), float(probs[1]), float(probs[2])

        conf_raw = max(0.0, (float(np.max(probs)) - 1.0 / 3.0) * 3.0)
        if n < _NEURAL_TAB_SHADOW:
            conf   = min(conf_raw, _NEURAL_CONF_CAP_LOW)
            status = "SHADOW"
            warn.append(f"shadow_only — confidence cap {_NEURAL_CONF_CAP_LOW}")
        else:
            conf   = min(conf_raw, _NEURAL_CONF_CAP_HIGH)
            status = "OBSERVATION"
        conf = min(conf, _NEURAL_CONF_MAX)

        # ── Contrarian mode : si WR directionnel < 33% sur N≥50, inverser UP↔DOWN ──
        contrarian_active = False
        dir_wr, n_dir = _compute_dir_winrate(self.name, hz)
        if dir_wr is not None and n_dir >= 50 and dir_wr < 0.33:
            pu, pd = pd, pu
            contrarian_active = True
            status = f"CONTRARIAN_{status}"
            warn.append(f"CONTRARIAN_MODE — WR_dir={dir_wr:.1%}/{n_dir} signaux UP↔DOWN inversé")

        return ArenaOutput(
            model_name=self.name, version=self.version, timestamp=ts,
            horizon=hz, spot_at_prediction=spot,
            prob_up=round(pu, 3), prob_down=round(pd, 3), prob_range=round(pr, 3),
            confidence=round(conf, 3),
            dominant_scenario=_dominant_from_prob3(pu, pd, pr),
            data_coverage=round(min(1.0, n / _NEURAL_TAB_SHADOW), 3),
            top_factors=meta.get("top_features", [])[:4],
            warnings=warn, features_snapshot=f,
            explanation={
                "model_status":        status,
                "n_training_samples":  n,
                "val_winrate":         meta.get("val_winrate"),
                "raw_confidence":      round(conf_raw, 3),
                "contrarian_mode":     contrarian_active,
                "dir_winrate":         dir_wr,
                "dir_n":               n_dir,
            },
        )


# ─────────────────────────── Temporal Neural Engine ──────────────────────────

class TemporalNeuralEngine:
    """GRU léger sur les séquences de snapshots — dynamique temporelle.

    Entrée : 24 derniers snapshots (≈12h à intervalle 30min).
    Garde-fous :
      WARMING_UP         : N < 500 séquences
      SHADOW             : 500 ≤ N < 1500, confidence ≤ 60%
      OBSERVATION_AVANCEE: N ≥ 1500, confidence ≤ 70%
      INVALID            : val_winrate < 0.45 → prédictions non publiées
    """

    name    = _TEMPORAL_NAME
    version = _TEMPORAL_VERSION

    def __init__(self):
        self._models: Dict[str, _GRUModel] = {}
        self._meta:   Dict[str, dict]      = {}
        self._retrain_attempts:     Dict[str, int]   = {hz: 0 for hz in _HORIZONS}
        self._last_retrain_attempt: Dict[str, float] = {hz: 0.0 for hz in _HORIZONS}
        self._last_prediction_ts:   Dict[str, float] = {hz: 0.0 for hz in _HORIZONS}
        self._load_all()

    def _mpath(self, hz: str) -> str:
        return os.path.join(_NEURAL_MODEL_DIR, f"temporal_{hz}")

    def _metapath(self, hz: str) -> str:
        return os.path.join(_NEURAL_MODEL_DIR, f"temporal_{hz}_meta.json")

    def _load_all(self):
        for hz in _HORIZONS:
            m = _GRUModel(_TEMPORAL_FEATURES)
            if m.load(self._mpath(hz)):
                # Invalider le modèle si la dimension d'entrée a changé
                if m.Wz.shape[0] != _TEMPORAL_FEATURES:
                    log.warning(
                        f"[temporal] {hz} — dimension Wz {m.Wz.shape[0]} ≠ "
                        f"{_TEMPORAL_FEATURES}, modèle invalidé (retraining)"
                    )
                else:
                    self._models[hz] = m
            mp = Path(self._metapath(hz))
            if mp.exists():
                try:
                    with open(mp) as f:
                        self._meta[hz] = json.load(f)
                except Exception:
                    pass

    def _save(self, hz: str, model: _GRUModel, meta: dict):
        Path(_NEURAL_MODEL_DIR).mkdir(parents=True, exist_ok=True)
        model.save(self._mpath(hz))
        with open(self._metapath(hz), "w") as f:
            json.dump(meta, f, ensure_ascii=False)
        self._models[hz] = model
        self._meta[hz] = meta

    def _step_vec(self, f: dict, spot_ret: float = 0.0) -> Optional[np.ndarray]:
        try:
            gex  = float(f.get("gex_near", 0) or 0)
            dex  = f.get("dex_direction", "")
            mopi = float(f.get("mopi_score", 50) or 50)
            iv   = float(f.get("iv_rank", 50) or 50)
            fund = float(f.get("funding_rate", 0) or 0)
            flip = float(f.get("flip_distance_pct", 0) or 0)
            reg  = str(f.get("gex_regime", "NEUTRE"))
            div_type = float(f.get("mopi_div_type_enc", 0.0) or 0.0)
            div_str  = float(f.get("mopi_div_strength", 0.0) or 0.0)
            return np.array([
                float(np.clip(spot_ret, -0.10, 0.10)) / 0.10,
                float(np.clip(gex / 5e9, -1.0, 1.0)),
                1.0 if "BULLISH" in str(dex) else (-1.0 if "BEARISH" in str(dex) else 0.0),
                (mopi - 50.0) / 50.0,
                (iv   - 50.0) / 50.0,
                float(np.clip(fund / 0.005, -2.0, 2.0)) / 2.0,
                float(np.clip(flip / 0.20, -1.0, 1.0)),
                1.0 if reg == "STABILISANT" else (-1.0 if reg == "AMPLIFICATEUR" else 0.0),
                float(np.clip(div_type, -1.0, 1.0)),
                float(np.clip(div_str / 0.030, 0.0, 1.0)),
            ], dtype=np.float64)
        except Exception:
            return None

    def _ordered_preds(self, hz: str, limit: int = 3000) -> list:
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.timestamp, mp.spot_at_prediction, mp.features_json,
                          mo.realized_direction, mo.return_pct
                   FROM model_predictions mp
                   LEFT JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = ? AND mp.horizon = ? AND mp.is_seed = 0
                   ORDER BY mp.timestamp ASC LIMIT ?""",
                (_EXPERT_NAME, hz, limit),
            ).fetchall()
        out = []
        for r in rows:
            try:
                feat = json.loads(r["features_json"] or "{}")
                out.append({
                    "spot":       float(r["spot_at_prediction"] or 0),
                    "feat":       feat,
                    "label":      r["realized_direction"],
                    "return_pct": float(r["return_pct"]) if r["return_pct"] is not None else 0.0,
                })
            except Exception:
                pass
        return out

    def _build_sequences(self, rows: list):
        sl = _NEURAL_SEQ_LEN
        seqs, labels, rets = [], [], []
        for end in range(sl - 1, len(rows)):
            row = rows[end]
            if not row.get("label"):
                continue
            li = _LABEL_IDX.get(row["label"])
            if li is None:
                continue
            chunk = rows[end - sl + 1: end + 1]
            vecs, ok = [], True
            prev_spot = chunk[0]["spot"]
            for i, r in enumerate(chunk):
                sp   = r["spot"]
                sret = (sp - prev_spot) / prev_spot if (i > 0 and prev_spot > 0) else 0.0
                v    = self._step_vec(r["feat"], sret)
                if v is None:
                    ok = False; break
                vecs.append(v); prev_spot = sp
            if ok and len(vecs) == sl:
                seqs.append(np.array(vecs, dtype=np.float64))
                labels.append(li)
                rets.append(float(row.get("return_pct") or 0.0))
        if not seqs:
            return (np.empty((0, sl, _TEMPORAL_FEATURES)),
                    np.empty(0, dtype=int),
                    np.empty(0, dtype=np.float64))
        return (np.array(seqs, dtype=np.float64),
                np.array(labels, dtype=int),
                np.array(rets,   dtype=np.float64))

    def _stale(self, hz: str) -> bool:
        meta = self._meta.get(hz, {})
        if not self._models.get(hz, _GRUModel(_TEMPORAL_FEATURES))._trained:
            return True
        return (time.time() - meta.get("trained_at", 0)) > _NEURAL_RETRAIN_H * 3600

    def _retrain(self, hz: str):
        t_start = time.time()
        self._last_retrain_attempt[hz] = t_start
        self._retrain_attempts[hz] = self._retrain_attempts.get(hz, 0) + 1
        try:
            rows    = self._ordered_preds(hz)
            X, y, r_arr = self._build_sequences(rows)
            n = len(X)
            if n < _TEMPORAL_WARMUP:
                _log_retrain_attempt(_TEMPORAL_NAME, hz, "skipped_no_data", n, time.time() - t_start)
                return

            split = int(n * 0.70)
            if split < 100 or (n - split) < 50:
                _log_retrain_attempt(_TEMPORAL_NAME, hz, "skipped_split_too_small", n, time.time() - t_start)
                return

            Xtr, ytr = X[:split], y[:split]
            Xva, yva = X[split:], y[split:]
            rva      = r_arr[split:]

            mu  = Xtr.reshape(-1, _TEMPORAL_FEATURES).mean(0)
            std = Xtr.reshape(-1, _TEMPORAL_FEATURES).std(0) + 1e-8
            Xtr_n = (Xtr - mu) / std
            Xva_n = (Xva - mu) / std

            # Class weights — corrige le collapse sur RANGE du GRU
            cw = _class_weights_from_labels(ytr)
            model   = _GRUModel(_TEMPORAL_FEATURES, hidden_dim=32, seed=int(time.time()) % 10000)
            metrics = model.train(Xtr_n, ytr, Xva_n, yva, class_weights=cw)
            val_wr  = metrics.get("val_winrate", 0.0)

            # Compute val EV and PF using neural model's predictions on val set
            val_probs    = np.array([model.predict_proba(seq_n) for seq_n in Xva_n])
            val_pred_idx = np.argmax(val_probs, axis=1)
            dir_adj_rets = [
                float(ret) if _IDX_LABEL[int(pi)] == "UP"
                else (-float(ret) if _IDX_LABEL[int(pi)] == "DOWN" else 0.0)
                for pi, ret in zip(val_pred_idx, rva)
            ]
            val_ev  = round(float(np.mean(dir_adj_rets)), 4) if dir_adj_rets else None
            wins    = [r for r in dir_adj_rets if r > 0]
            losses  = [r for r in dir_adj_rets if r < 0]
            val_pf  = round(sum(wins) / abs(sum(losses)), 3) if losses else None

            duration_s = round(time.time() - t_start, 2)
            meta = {
                "trained_at":         int(time.time()),
                "n_training_samples": n,
                "train_period":       {"n_train": split, "n_val": n - split},
                "val_winrate":        val_wr,
                "val_ev":             val_ev,
                "val_pf":             val_pf,
                "val_n":              metrics.get("val_n", 0),
                "confusion_matrix":   metrics.get("confusion_matrix", []),
                "model_status":       "OBSERVATION" if val_wr >= 0.45 else "INVALID",
                "seq_len":            _NEURAL_SEQ_LEN,
                "scaler":             {"mean": mu.tolist(), "std": std.tolist()},
            }
            self._save(hz, model, meta)

            with _conn() as c:
                c.execute(
                    """INSERT INTO neural_training_log
                       (model_name, horizon, trained_at, n_samples, val_winrate, val_ev, val_pf, duration_s, status, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (_TEMPORAL_NAME, hz, int(time.time()), n, val_wr, val_ev, val_pf, duration_s, "success", "auto"),
                )
                c.commit()
            _log_retrain_attempt(_TEMPORAL_NAME, hz, "success", n, duration_s)

        except Exception as e:
            _log_retrain_attempt(_TEMPORAL_NAME, hz, "failed", 0, time.time() - t_start, str(e)[:500])
            raise

    def _recent_seq(self, current_feat: dict, current_spot: float, hz: str) -> Optional[np.ndarray]:
        sl = _NEURAL_SEQ_LEN
        with _conn() as c:
            rows = c.execute(
                """SELECT spot_at_prediction, features_json FROM model_predictions
                   WHERE model_name = ? AND horizon = ? AND is_seed = 0
                   ORDER BY timestamp DESC LIMIT ?""",
                (_EXPERT_NAME, hz, sl - 1),
            ).fetchall()
        history = list(reversed(rows))
        vecs, prev_spot = [], (float(history[0]["spot_at_prediction"]) if history else current_spot)
        for i, row in enumerate(history):
            try:
                f  = json.loads(row["features_json"] or "{}")
                sp = float(row["spot_at_prediction"] or prev_spot)
                sr = (sp - prev_spot) / prev_spot if (i > 0 and prev_spot > 0) else 0.0
                v  = self._step_vec(f, sr)
                if v is not None:
                    vecs.append(v); prev_spot = sp
            except Exception:
                continue

        sr_curr = (current_spot - prev_spot) / prev_spot if prev_spot > 0 else 0.0
        v_curr  = self._step_vec(current_feat, sr_curr)
        if v_curr is not None:
            vecs.append(v_curr)
        if len(vecs) < 4:
            return None
        while len(vecs) < sl:
            vecs.insert(0, vecs[0].copy())
        return np.array(vecs[-sl:], dtype=np.float64)

    def predict(self, spot: float, features_snapshot: dict) -> List[ArenaOutput]:
        return [self._predict_hz(spot, features_snapshot, hz) for hz in _HORIZONS]

    def _predict_hz(self, spot: float, f: dict, hz: str) -> ArenaOutput:
        ts   = datetime.now(timezone.utc).isoformat()
        warn: List[str] = []
        self._last_prediction_ts[hz] = time.time()

        if self._stale(hz):
            try:
                self._retrain(hz)
            except Exception as e:
                log.warning(f"[temporal_neural] retrain {hz}: {e}")

        meta = self._meta.get(hz, {})
        n    = meta.get("n_training_samples", 0)

        if n < _TEMPORAL_WARMUP:
            warn.append(f"WARMING_UP — {n}/{_TEMPORAL_WARMUP} séquences requises")
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE",
                data_coverage=round(n / _TEMPORAL_WARMUP, 3),
                top_factors=[], warnings=warn, features_snapshot=f,
                explanation={"model_status": "WARMING_UP", "n_sequences": n},
            )

        if meta.get("model_status") == "INVALID":
            warn.append("WARMING_UP — val_winrate < 0.45, prédictions non publiées")
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=warn, features_snapshot=f,
                explanation={"model_status": "INVALID"},
            )

        model = self._models.get(hz)
        if model is None or not model._trained:
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=["NOT_READY"], features_snapshot=f,
                explanation={"model_status": "NOT_READY"},
            )

        seq = self._recent_seq(f, spot, hz)
        if seq is None:
            return ArenaOutput(
                model_name=self.name, version=self.version, timestamp=ts,
                horizon=hz, spot_at_prediction=spot,
                prob_up=0.333, prob_down=0.333, prob_range=0.334, confidence=0.0,
                dominant_scenario="RANGE", data_coverage=0.0,
                top_factors=[], warnings=["INSUFFICIENT_HISTORY"], features_snapshot=f,
                explanation={"model_status": "INSUFFICIENT_HISTORY"},
            )

        sc  = meta.get("scaler", {})
        mu  = np.array(sc.get("mean", np.zeros(_TEMPORAL_FEATURES)))
        std = np.array(sc.get("std",  np.ones(_TEMPORAL_FEATURES)))
        seq_n = (seq - mu) / (std + 1e-8)

        probs = model.predict_proba(seq_n)
        pu, pd, pr = float(probs[0]), float(probs[1]), float(probs[2])

        conf_raw = max(0.0, (float(np.max(probs)) - 1.0 / 3.0) * 3.0)
        if n < _TEMPORAL_SHADOW:
            conf   = min(conf_raw, _TEMPORAL_CONF_CAP_LOW)
            status = "SHADOW"
            warn.append(f"shadow_only — confidence cap {_TEMPORAL_CONF_CAP_LOW}")
        else:
            conf   = min(conf_raw, _TEMPORAL_CONF_CAP)
            status = "OBSERVATION_AVANCEE"

        # ── Contrarian mode : si WR directionnel < 33% sur N≥50, inverser UP↔DOWN ──
        contrarian_active = False
        dir_wr, n_dir = _compute_dir_winrate(self.name, hz)
        if dir_wr is not None and n_dir >= 50 and dir_wr < 0.33:
            pu, pd = pd, pu
            contrarian_active = True
            status = f"CONTRARIAN_{status}"
            warn.append(f"CONTRARIAN_MODE — WR_dir={dir_wr:.1%}/{n_dir} signaux UP↔DOWN inversé")

        return ArenaOutput(
            model_name=self.name, version=self.version, timestamp=ts,
            horizon=hz, spot_at_prediction=spot,
            prob_up=round(pu, 3), prob_down=round(pd, 3), prob_range=round(pr, 3),
            confidence=round(conf, 3),
            dominant_scenario=_dominant_from_prob3(pu, pd, pr),
            data_coverage=round(min(1.0, n / _TEMPORAL_SHADOW), 3),
            top_factors=[], warnings=warn, features_snapshot=f,
            explanation={
                "model_status":         status,
                "n_training_sequences": n,
                "val_winrate":          meta.get("val_winrate"),
                "seq_len_used":         len(seq),
                "seq_len_expected":     _NEURAL_SEQ_LEN,
                "contrarian_mode":      contrarian_active,
                "dir_winrate":          dir_wr,
                "dir_n":                n_dir,
            },
        )


def refresh_ame_clusters() -> int:
    """Recalcule les stats de chaque cluster AME depuis les outcomes enregistrés.

    Lit les prédictions AME dont l'explanation_json contient 'ame_cluster_id',
    recalcule WR/EV/PF par cluster×horizon, met à jour adaptive_memory_clusters.
    Retourne le nombre de clusters mis à jour.
    """
    now = int(time.time())

    with _conn() as c:
        rows = c.execute(
            """SELECT mp.explanation_json, mp.horizon,
                      mo.is_correct, mo.direction_adjusted_return, mo.return_pct,
                      mp.dominant_scenario
               FROM model_predictions mp
               JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mp.model_name = ? AND mp.is_seed = 0
                 AND mo.realized_direction IS NOT NULL""",
            (_AME_NAME,),
        ).fetchall()

    from collections import defaultdict as _dd

    cluster_data: dict = _dd(lambda: {"hits": 0, "total": 0, "returns": [], "regime": ""})

    for r in rows:
        try:
            expl = json.loads(r["explanation_json"] or "{}")
            cid  = expl.get("ame_cluster_id")
            if not cid:
                continue
        except Exception:
            continue

        key = (cid, r["horizon"])
        cluster_data[key]["total"] += 1
        if r["is_correct"] == 1:
            cluster_data[key]["hits"] += 1

        adj = (
            r["direction_adjusted_return"]
            if r["direction_adjusted_return"] is not None
            else _direction_adjusted(r["return_pct"], r["dominant_scenario"])
        )
        if adj is not None:
            cluster_data[key]["returns"].append(adj)

        # Extract regime from cluster_id (first segment)
        cluster_data[key]["regime"] = cid.split("|")[0] if "|" in cid else ""

    n_updated = 0
    with _conn() as c:
        for (cid, hz), data in cluster_data.items():
            n = data["total"]
            if n == 0:
                continue
            winrate  = data["hits"] / n
            returns  = data["returns"]
            ev       = float(np.mean(returns)) if returns else None
            gains    = [r for r in returns if r > 0]
            losses   = [r for r in returns if r < 0]
            avg_win  = float(np.mean(gains))                   if gains   else None
            avg_loss = float(np.mean([abs(r) for r in losses])) if losses else None
            pf       = round(sum(gains) / sum(abs(r) for r in losses), 4) if losses else None

            # Reliability score basé sur WR et EV
            wr_factor = (winrate - 0.5) * 2.0
            ev_factor = float(np.clip((ev or 0.0) / 2.0, -0.5, 0.5))
            reliability = float(np.clip(1.0 + wr_factor * 0.3 + ev_factor * 0.2, 0.5, 1.5))

            c.execute(
                """INSERT OR REPLACE INTO adaptive_memory_clusters
                   (cluster_id, horizon, regime, feature_signature, n_outcomes,
                    winrate, ev_mean, profit_factor, avg_win, avg_loss,
                    reliability_score, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, hz, data["regime"], cid, n,
                 round(winrate, 4), round(ev, 4) if ev is not None else None,
                 pf, round(avg_win, 4) if avg_win is not None else None,
                 round(avg_loss, 4) if avg_loss is not None else None,
                 round(reliability, 4), now),
            )
            n_updated += 1
        c.commit()

    return n_updated


def _ame_promotion_check() -> dict:
    """Critères de promotion spécifiques à l'AME.

    Requis :
      - 300 outcomes 4h
      - 150 outcomes 24h
      - 75 outcomes 72h
      - EV positif (tous horizons)
      - Profit factor > 1.2
      - Winrate supérieur à Auto-Calibrated ET ML Research
    """
    stats     = _model_stats(_AME_NAME, days=30)
    autocal_s = _model_stats(_AUTOCAL_NAME, days=30)
    ml_s      = _model_stats(_ML_NAME, days=30)

    n_4h  = stats.get("4h",  {}).get("n_signals", 0)
    n_24h = stats.get("24h", {}).get("n_signals", 0)
    n_72h = stats.get("72h", {}).get("n_signals", 0)

    ev_ok  = all(stats.get(hz, {}).get("ev_mean", -1) > 0 for hz in _HORIZONS if stats.get(hz, {}).get("n_signals", 0) > 0)
    pf_ok  = all(
        (stats.get(hz, {}).get("profit_factor") or 0) > _AME_MIN_PROFIT_FACTOR
        for hz in _HORIZONS if stats.get(hz, {}).get("n_signals", 0) > 0
    )

    def _avg_wr(s: dict) -> Optional[float]:
        wrs = [s.get(hz, {}).get("winrate", 0) for hz in _HORIZONS if s.get(hz, {}).get("n_signals", 0) > 0]
        return sum(wrs) / len(wrs) if wrs else None

    ame_wr = _avg_wr(stats)
    acal_wr = _avg_wr(autocal_s)
    ml_wr   = _avg_wr(ml_s)

    beats_autocal = (ame_wr is not None and acal_wr is not None and ame_wr > acal_wr)
    beats_ml      = (ame_wr is not None and ml_wr   is not None and ame_wr > ml_wr)

    criteria = {
        "300_outcomes_4h":  n_4h  >= _AME_MIN_OUTCOMES_4H,
        "150_outcomes_24h": n_24h >= _AME_MIN_OUTCOMES_24H,
        "75_outcomes_72h":  n_72h >= _AME_MIN_OUTCOMES_72H,
        "ev_positive":      ev_ok,
        "profit_factor_1_2": pf_ok,
        "beats_auto_calibrated": beats_autocal,
        "beats_ml_research":     beats_ml,
    }

    with _conn() as c:
        fts_row = c.execute(
            "SELECT MIN(timestamp) as ts FROM model_predictions WHERE model_name=?",
            (_AME_NAME,),
        ).fetchone()
    first_ts = fts_row["ts"] if fts_row and fts_row["ts"] else int(time.time())
    days_of_data = (int(time.time()) - first_ts) / 86400

    return {
        "total_outcomes":   n_4h + n_24h + n_72h,
        "n_per_horizon":    {"4h": n_4h, "24h": n_24h, "72h": n_72h},
        "days_of_data":     round(days_of_data, 1),
        "criteria":         criteria,
        "promotion_ready":  all(criteria.values()),
    }


# ─────────────────────────── Model Arena Orchestrator ────────────────────────

class ModelArena:
    def __init__(self):
        self._expert   = ExpertRulesEngine()
        self._autocal  = AutoCalibratedEngine()
        self._ml       = MLResearchEngine()
        self._expert2  = Expert2CrashGateEngine()
        self._naive    = NaiveBaselineEngine()
        self._acrs       = AutoCalibratedRegimeShadowEngine()
        self._ame        = AdaptiveMemoryEngine()
        self._neural_tab = NeuralTabularEngine()
        self._temporal   = TemporalNeuralEngine()
        self._mde        = MopiDivergenceEngine()
        self._bme        = get_bme()

        # Constantes exposées pour les endpoints
        self._MIN_DAYS_PROMOTION     = _MIN_DAYS_PROMOTION
        self._MIN_OUTCOMES_PROMOTION = _MIN_OUTCOMES_PROMOTION
        self._MIN_OUTCOMES_COLLECTING = _MIN_OUTCOMES_COLLECTING

    def run_all(
        self,
        spot: float,
        pe_output: ProbabilityEngineOutput,
        features_snapshot: dict,
    ) -> Dict[str, List[ArenaOutput]]:
        results: Dict[str, List[ArenaOutput]] = {}

        for engine_name, run_fn in [
            (_EXPERT_NAME,   lambda: self._expert.predict(spot, pe_output, features_snapshot)),
            (_AUTOCAL_NAME,  lambda: self._autocal.predict(spot, pe_output, features_snapshot)),
            (_ML_NAME,       lambda: self._ml.predict(spot, features_snapshot)),
            (_EXPERT2_NAME,  lambda: self._expert2.predict(spot, pe_output, features_snapshot)),
            (_NAIVE_NAME,    lambda: self._naive.predict(spot)),
            (_ACRS_NAME,     lambda: self._acrs.predict(spot, pe_output, features_snapshot)),
            (_AME_NAME,        lambda: self._ame.predict(spot, features_snapshot)),
            (_NEURAL_TAB_NAME, lambda: self._neural_tab.predict(spot, features_snapshot)),
            (_TEMPORAL_NAME,   lambda: self._temporal.predict(spot, features_snapshot)),
            (_MDE_NAME,        lambda: self._mde.predict(spot)),
            (_BME_NAME,        lambda: self._bme.predict(spot, dict(features_snapshot))),
        ]:
            try:
                outputs = run_fn()
                for out in outputs:
                    if engine_name not in (_ML_NAME, _NAIVE_NAME):
                        out.features_snapshot = features_snapshot
                    _save_prediction(out)
                results[engine_name] = outputs
            except Exception as e:
                log.error(f"[arena] {engine_name} error: {e}")
                results[engine_name] = []

        return results


# ─────────────────────────── Class Weights & Contrarian Helpers ─────────────

def _class_weights_from_labels(y: np.ndarray) -> np.ndarray:
    """Poids inversement proportionnels à la fréquence de chaque classe [UP/DOWN/RANGE].

    Corrige le class imbalance qui provoque le collapse du modèle sur RANGE.
    Normalisés pour que la moyenne = 1.0.
    """
    from collections import Counter
    counts = Counter(y.tolist())
    total = len(y)
    n_cls = 3
    cw = np.array([
        total / (n_cls * max(counts.get(i, 1), 1))
        for i in range(n_cls)
    ], dtype=np.float64)
    return cw / (cw.mean() + 1e-10)


def _compute_dir_winrate(model_name: str, hz: str, n_recent: int = 100) -> Tuple[Optional[float], int]:
    """Winrate directionnel récent (seulement UP/DOWN, exclu RANGE).

    Retourne (winrate, n_signals). Si n < 20 → (None, 0).
    Utilisé pour détecter si un moteur prédit systématiquement à l'envers.
    """
    with _conn() as c:
        rows = c.execute(
            """SELECT mp.dominant_scenario, mo.realized_direction
               FROM model_predictions mp
               JOIN model_outcomes mo ON mp.id=mo.prediction_id AND mo.horizon=mp.horizon
               WHERE mp.model_name=? AND mp.horizon=?
                 AND mp.dominant_scenario IN ('UP','DOWN')
                 AND mo.realized_direction IN ('UP','DOWN')
                 AND mp.is_seed=0
               ORDER BY mp.timestamp DESC LIMIT ?""",
            (model_name, hz, n_recent),
        ).fetchall()
    if not rows or len(rows) < 20:
        return None, 0
    n = len(rows)
    correct = sum(1 for r in rows if r["dominant_scenario"] == r["realized_direction"])
    return round(correct / n, 4), n


# ─────────────────────────── Outcome Evaluation ──────────────────────────────

def evaluate_pending_outcomes(current_spot: float):
    """Évalue les prédictions ayant atteint leur horizon (4h/24h/72h)."""
    now = int(time.time())
    horizon_secs = {"4h": 4 * 3600, "24h": 24 * 3600, "72h": 72 * 3600}

    for hz, secs in horizon_secs.items():
        cutoff = now - secs

        with _conn() as c:
            rows = c.execute(
                """SELECT mp.id, mp.spot_at_prediction, mp.dominant_scenario,
                          mp.explanation_json, mp.model_name
                   FROM model_predictions mp
                   LEFT JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.horizon = ? AND mp.timestamp <= ?
                     AND mo.prediction_id IS NULL
                   LIMIT 100""",
                (hz, cutoff),
            ).fetchall()

        for row in rows:
            pred_id    = row["id"]
            spot_entry = row["spot_at_prediction"]
            dominant   = row["dominant_scenario"]
            model_name = row["model_name"]
            expl_str   = row["explanation_json"]

            if not spot_entry or spot_entry <= 0:
                continue

            ret_pct = (current_spot - spot_entry) / spot_entry * 100

            if ret_pct > _DIR_THRESHOLD_PCT:
                realized = "UP"
            elif ret_pct < -_DIR_THRESHOLD_PCT:
                realized = "DOWN"
            else:
                realized = "RANGE"

            is_correct = 1 if dominant == realized else 0

            # Direction-adjusted P&L: what a trader earns following this model's signal.
            # UP prediction → long position: gain if BTC rises, loss if BTC falls.
            # DOWN prediction → short position: gain if BTC falls, loss if BTC rises.
            # RANGE prediction → no position: 0 P&L.
            if dominant == "UP":
                dir_adj_ret = ret_pct
            elif dominant == "DOWN":
                dir_adj_ret = -ret_pct
            else:
                dir_adj_ret = 0.0

            # MAE/MFE from the perspective of the predicted trade direction
            if dominant == "UP":
                mae = round(max(0.0, -ret_pct), 4)
                mfe = round(max(0.0,  ret_pct), 4)
            elif dominant == "DOWN":
                mae = round(max(0.0,  ret_pct), 4)
                mfe = round(max(0.0, -ret_pct), 4)
            else:
                mae = round(abs(ret_pct), 4)
                mfe = 0.0

            with _conn() as c:
                c.execute(
                    """INSERT OR IGNORE INTO model_outcomes
                    (prediction_id, horizon, spot_entry, spot_exit, return_pct,
                     realized_direction, is_correct, mae, mfe, evaluated_at,
                     direction_adjusted_return)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (pred_id, hz, spot_entry, current_spot, ret_pct,
                     realized, is_correct, mae, mfe, now,
                     round(dir_adj_ret, 4)),
                )
                c.commit()

            # Auto-calibration feedback pour le moteur calibré
            if model_name == _AUTOCAL_NAME and expl_str:
                try:
                    expl = json.loads(expl_str)
                    AutoCalibratedEngine().calibrate_from_outcome(hz, bool(is_correct), expl)
                except Exception as e:
                    log.error(f"[arena] calibration error pred {pred_id}: {e}")


# ─────────────────────────── Arena Statistics ────────────────────────────────

def _wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson interval de confiance 95% pour un winrate k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return (max(0.0, round(centre - margin, 4)), min(1.0, round(centre + margin, 4)))


def _z_test_proportions(k1: int, n1: int, k2: int, n2: int) -> dict:
    """Test Z de différence de deux proportions (winrates)."""
    if n1 == 0 or n2 == 0:
        return {"z_score": None, "p_value": None, "is_significant": False}
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = (pooled * (1 - pooled) * (1 / n1 + 1 / n2)) ** 0.5
    if se == 0:
        return {"z_score": None, "p_value": None, "is_significant": False}
    z = (p1 - p2) / se
    # Approximation: p-value via erf pour éviter l'import scipy
    import math
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / 2 ** 0.5)))
    return {
        "z_score": round(z, 3),
        "p_value": round(p_value, 4),
        "is_significant": p_value < 0.05,
    }


def _direction_adjusted(return_pct: Optional[float], dominant_scenario: str) -> float:
    """Compute direction-adjusted P&L from raw BTC return and predicted direction."""
    if return_pct is None:
        return 0.0
    if dominant_scenario == "UP":
        return return_pct
    if dominant_scenario == "DOWN":
        return -return_pct
    return 0.0  # RANGE = no position


def _model_stats(model_name: str, days: int = 30) -> Dict[str, dict]:
    cutoff = int(time.time()) - days * 86400
    stats = {}

    for hz in _HORIZONS:
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.dominant_scenario, mp.confidence,
                          mo.is_correct, mo.return_pct,
                          mo.direction_adjusted_return, mo.realized_direction,
                          mo.mae, mo.mfe
                   FROM model_predictions mp
                   JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = ? AND mp.horizon = ? AND mp.timestamp >= ?
                   ORDER BY mp.timestamp DESC""",
                (model_name, hz, cutoff),
            ).fetchall()

        if not rows:
            stats[hz] = {"n_signals": 0, "status": "no_data"}
            continue

        n = len(rows)
        winrate = sum(1 for r in rows if r["is_correct"] == 1) / n

        # EV = direction-adjusted return (P&L if you follow each signal).
        # Falls back to raw return_pct for legacy rows that predate the migration.
        adj_returns = [
            r["direction_adjusted_return"]
            if r["direction_adjusted_return"] is not None
            else _direction_adjusted(r["return_pct"], r["dominant_scenario"])
            for r in rows
            if r["return_pct"] is not None
        ]
        ev = float(np.mean(adj_returns)) if adj_returns else 0.0
        median_ret = float(np.median(adj_returns)) if adj_returns else 0.0

        loss_streak = max_loss_streak = 0
        for r in rows:
            if r["is_correct"] == 0:
                loss_streak += 1
                max_loss_streak = max(max_loss_streak, loss_streak)
            else:
                loss_streak = 0

        neutral_rate = sum(1 for r in rows if r["realized_direction"] == "RANGE") / n
        recent = rows[:10]
        recent_wr = sum(1 for r in recent if r["is_correct"] == 1) / len(recent) if recent else 0.0
        losses = [r for r in adj_returns if r < 0]
        gains  = [r for r in adj_returns if r > 0]
        mae = float(np.mean([abs(r) for r in losses])) if losses else 0.0
        mfe = float(np.mean(gains)) if gains else 0.0

        sum_gains  = sum(gains)
        sum_losses = sum(abs(r) for r in losses)
        profit_factor = round(sum_gains / sum_losses, 3) if sum_losses > 0 else None

        # Sharpe simplifié : EV / std(returns) × √N
        std_all = float(np.std(adj_returns)) if len(adj_returns) > 1 else 0.0
        sharpe = round(ev / std_all * (len(adj_returns) ** 0.5), 3) if std_all > 0 else None

        # Sortino simplifié : EV / std(pertes) × √N
        std_down = float(np.std(losses)) if len(losses) > 1 else 0.0
        sortino = round(ev / std_down * (len(adj_returns) ** 0.5), 3) if std_down > 0 else None

        # Expectancy = (WR × avg_win) - ((1-WR) × avg_loss)
        expectancy = round(winrate * mfe - (1 - winrate) * mae, 4)

        # Max drawdown cumulatif (peak-to-trough)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in adj_returns:
            cum += r
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        max_drawdown = round(max_dd, 4)

        # Intervalle de confiance Wilson 95%
        n_wins_int = sum(1 for r in adj_returns if r > 0)
        n_total_int = len(adj_returns)
        ci_lower, ci_upper = _wilson_ci(n_wins_int, n_total_int)
        margin_of_error = round((ci_upper - ci_lower) / 2, 4)
        is_noisy = margin_of_error >= abs(winrate - 0.5) or n < 30

        stats[hz] = {
            "n_signals": n,
            "winrate": round(winrate, 3),
            "ev_mean": round(ev, 3),
            "median_return": round(median_ret, 3),
            "max_drawdown_streak": max_loss_streak,
            "max_drawdown_cumulative": max_drawdown,
            "mae": round(mae, 3),
            "mfe": round(mfe, 3),
            "avg_win": round(mfe, 3),
            "avg_loss": round(mae, 3),
            "n_wins": len(gains),
            "n_losses": len(losses),
            "profit_factor": profit_factor,
            "sharpe": sharpe,
            "sortino": sortino,
            "expectancy": expectancy,
            "neutral_signal_rate": round(neutral_rate, 3),
            "recent_winrate_10": round(recent_wr, 3),
            "ci_lower": ci_lower,
            "ci_upper": ci_upper,
            "margin_of_error": margin_of_error,
            "is_noisy": is_noisy,
            "status": "ok",
        }

    return stats


def _current_predictions(model_name: str) -> Dict[str, dict]:
    result = {}
    for hz in _HORIZONS:
        with _conn() as c:
            row = c.execute(
                """SELECT prob_up, prob_down, prob_range, confidence, dominant_scenario,
                          data_coverage, model_version, timestamp, spot_at_prediction,
                          explanation_json
                   FROM model_predictions
                   WHERE model_name = ? AND horizon = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (model_name, hz),
            ).fetchone()
        if row:
            d = dict(row)
            try:
                d["explanation"] = json.loads(d.pop("explanation_json") or "{}")
            except Exception:
                d["explanation"] = {}
                d.pop("explanation_json", None)
            result[hz] = d
    return result


def _promotion_check(model_name: str) -> dict:
    with _conn() as c:
        row_n = c.execute(
            "SELECT COUNT(*) as n FROM model_predictions mp "
            "JOIN model_outcomes mo ON mp.id = mo.prediction_id "
            "WHERE mp.model_name = ?",
            (model_name,),
        ).fetchone()
        total_outcomes = row_n["n"] if row_n else 0

        row_ts = c.execute(
            "SELECT MIN(timestamp) as ts FROM model_predictions WHERE model_name = ?",
            (model_name,),
        ).fetchone()
        first_ts = row_ts["ts"] if row_ts and row_ts["ts"] else int(time.time())

    days_of_data = (int(time.time()) - first_ts) / 86400
    stats = _model_stats(model_name, days=30)
    expert_stats = _model_stats(_EXPERT_NAME, days=30)

    criteria = {
        "min_100_outcomes": total_outcomes >= _MIN_OUTCOMES_PROMOTION,
        "min_14_days": days_of_data >= _MIN_DAYS_PROMOTION,
    }
    for hz in _HORIZONS:
        s = stats.get(hz, {})
        es = expert_stats.get(hz, {})
        if s.get("n_signals", 0) > 0:
            criteria[f"ev_positive_{hz}"] = s.get("ev_mean", 0) > _EV_THRESHOLD
        if s.get("n_signals", 0) > 0 and es.get("n_signals", 0) > 0:
            criteria[f"better_winrate_{hz}"] = (
                s.get("winrate", 0) > es.get("winrate", 0)
            )

    return {
        "total_outcomes": total_outcomes,
        "days_of_data": round(days_of_data, 1),
        "criteria": criteria,
        "promotion_ready": all(criteria.values()) if criteria else False,
    }


def _arena_status(
    mn: str,
    n_total: int,
    perf: dict,
    expert_perf: dict,
) -> str:
    """Calcule le statut d'un moteur selon son N et ses performances."""
    if mn == _EXPERT_NAME:
        return "actif"
    if mn == _NAIVE_NAME:
        return "baseline" if n_total >= _MIN_OUTCOMES_COLLECTING else "collecting"
    if n_total < _MIN_OUTCOMES_COLLECTING:
        return "collecting"
    if n_total < _MIN_OUTCOMES_PROMOTION:
        return "evaluating"
    expert_wrs = [
        expert_perf.get(hz, {}).get("winrate", 0)
        for hz in _HORIZONS
        if isinstance(expert_perf.get(hz), dict) and expert_perf[hz].get("n_signals", 0) > 0
    ]
    model_wrs = [
        perf.get(hz, {}).get("winrate", 0)
        for hz in _HORIZONS
        if isinstance(perf.get(hz), dict) and perf[hz].get("n_signals", 0) > 0
    ]
    if expert_wrs and model_wrs:
        return (
            "outperforming"
            if (sum(model_wrs) / len(model_wrs)) > (sum(expert_wrs) / len(expert_wrs))
            else "underperforming"
        )
    return "evaluating"


def _best_model(days: int = 30) -> str:
    """Classe par EV moyen (critère principal) — winrate seul est trompeur."""
    best_name, best_score = _EXPERT_NAME, -999.0
    for mn in [_EXPERT_NAME, _EXPERT2_NAME, _AUTOCAL_NAME, _NAIVE_NAME]:
        stats = _model_stats(mn, days=days)
        n_total = sum(s.get("n_signals", 0) for s in stats.values() if isinstance(s, dict))
        if n_total < _MIN_OUTCOMES_COLLECTING:
            continue
        evs = [s["ev_mean"] for s in stats.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        score = float(np.mean(evs)) if evs else -999.0
        if score > best_score:
            best_score, best_name = score, mn
    return best_name


def snapshot_arena_performance() -> int:
    """Capture un snapshot des performances actuelles dans arena_performance_history.

    Appelé toutes les 30 min par le worker _arena_performance_snapshotter.
    Ne jamais écraser l'historique : chaque appel ajoute de nouvelles lignes.
    Retourne le nombre de lignes insérées.
    """
    now = int(time.time())
    all_models = [_EXPERT_NAME, _EXPERT2_NAME, _NAIVE_NAME, _AUTOCAL_NAME, _ML_NAME, _ACRS_NAME, _AME_NAME, _MDE_NAME]
    windows = [
        ("global", 30 * 86400),
        ("7d",      7 * 86400),
        ("24h",    24 * 3600),
    ]

    rows_to_insert = []

    for model_name in all_models:
        for window_name, window_secs in windows:
            cutoff = now - window_secs
            for hz in _HORIZONS:
                with _conn() as c:
                    db_rows = c.execute(
                        """SELECT mo.is_correct, mo.direction_adjusted_return,
                                  mo.return_pct, mp.dominant_scenario
                           FROM model_predictions mp
                           JOIN model_outcomes mo
                             ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                           WHERE mp.model_name = ?
                             AND mp.horizon = ?
                             AND mp.timestamp >= ?
                             AND mp.is_seed = 0""",
                        (model_name, hz, cutoff),
                    ).fetchall()

                if not db_rows:
                    continue

                n = len(db_rows)
                winrate = sum(1 for r in db_rows if r["is_correct"] == 1) / n

                adj_returns = [
                    r["direction_adjusted_return"]
                    if r["direction_adjusted_return"] is not None
                    else _direction_adjusted(r["return_pct"], r["dominant_scenario"])
                    for r in db_rows
                    if r["return_pct"] is not None
                ]

                if not adj_returns:
                    continue

                ev       = float(np.mean(adj_returns))
                gains    = [r for r in adj_returns if r > 0]
                losses   = [r for r in adj_returns if r < 0]
                avg_win  = float(np.mean(gains))                  if gains   else 0.0
                avg_loss = float(np.mean([abs(r) for r in losses])) if losses else 0.0
                sum_gains  = sum(gains)
                sum_losses = sum(abs(r) for r in losses)
                pf = round(sum_gains / sum_losses, 4) if sum_losses > 0 else None

                rows_to_insert.append((
                    now, model_name, hz, window_name,
                    n, round(winrate, 4), round(ev, 4),
                    pf, round(avg_win, 4), round(avg_loss, 4),
                    now,
                ))

    if rows_to_insert:
        with _conn() as c:
            c.executemany(
                """INSERT INTO arena_performance_history
                   (ts, model_name, horizon, window, n_outcomes, winrate, ev_mean,
                    profit_factor, avg_win, avg_loss, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows_to_insert,
            )
            c.commit()

    log.info(f"[arena] performance snapshot: {len(rows_to_insert)} rows saved")
    return len(rows_to_insert)


def get_arena_performance_history(days: int = 7) -> dict:
    """Série temporelle des performances pour /api/model_arena/performance_history."""
    now    = int(time.time())
    cutoff = now - days * 86400
    all_models = [_EXPERT_NAME, _EXPERT2_NAME, _NAIVE_NAME, _AUTOCAL_NAME, _ML_NAME, _ACRS_NAME, _AME_NAME, _MDE_NAME]

    with _conn() as c:
        rows = c.execute(
            """SELECT ts, model_name, horizon, n_outcomes,
                      winrate, ev_mean, profit_factor, avg_win, avg_loss
               FROM arena_performance_history
               WHERE ts >= ? AND window = 'global'
               ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()

        hist_meta = c.execute(
            "SELECT MIN(ts) as first_ts, COUNT(*) as n FROM arena_performance_history"
        ).fetchone()

    # Organiser : model → horizon → liste de points
    series: Dict[str, Dict[str, list]] = {
        mn: {hz: [] for hz in _HORIZONS}
        for mn in all_models
    }
    for r in rows:
        mn = r["model_name"]
        hz = r["horizon"]
        if mn in series and hz in series[mn]:
            series[mn][hz].append({
                "ts":            r["ts"],
                "n_outcomes":    r["n_outcomes"],
                "winrate":       r["winrate"],
                "ev_mean":       r["ev_mean"],
                "profit_factor": r["profit_factor"],
                "avg_win":       r["avg_win"],
                "avg_loss":      r["avg_loss"],
            })

    # Seuils pour le calcul de tendance
    cutoff_24h = now - 24 * 3600
    cutoff_48h = now - 48 * 3600

    models_out = {}
    for mn in all_models:
        trends: Dict[str, dict] = {}
        for hz in _HORIZONS:
            pts      = series[mn][hz]
            recent   = [p for p in pts if p["ts"] >= cutoff_24h]
            previous = [p for p in pts if cutoff_48h <= p["ts"] < cutoff_24h]

            wr_now  = float(np.mean([p["winrate"] for p in recent   if p["winrate"] is not None])) if recent   else None
            wr_prev = float(np.mean([p["winrate"] for p in previous if p["winrate"] is not None])) if previous else None
            ev_now  = float(np.mean([p["ev_mean"] for p in recent   if p["ev_mean"] is not None])) if recent   else None
            ev_prev = float(np.mean([p["ev_mean"] for p in previous if p["ev_mean"] is not None])) if previous else None
            n_now   = recent[-1]["n_outcomes"] if recent else 0

            if wr_now is None or wr_prev is None:
                direction = "collecting"
            elif wr_now > wr_prev + 0.01:
                direction = "improving"
            elif wr_now < wr_prev - 0.01:
                direction = "degrading"
            else:
                direction = "stable"

            trends[hz] = {
                "direction": direction,
                "wr_now":   round(wr_now,  4) if wr_now  is not None else None,
                "wr_prev":  round(wr_prev, 4) if wr_prev is not None else None,
                "ev_now":   round(ev_now,  4) if ev_now  is not None else None,
                "ev_prev":  round(ev_prev, 4) if ev_prev is not None else None,
                "n_now":    n_now,
            }

        # Tendance globale (agrégat des 3 horizons)
        dirs = [trends[hz]["direction"] for hz in _HORIZONS
                if trends[hz]["direction"] not in ("collecting",)]
        if not dirs:
            global_dir = "collecting"
        elif dirs.count("improving") > dirs.count("degrading"):
            global_dir = "improving"
        elif dirs.count("degrading") > dirs.count("improving"):
            global_dir = "degrading"
        else:
            global_dir = "stable"

        # Sparkline agrégée : un point par timestamp, WR moyen sur les 3 horizons
        sparkline_by_ts: Dict[int, List[float]] = {}
        for hz in _HORIZONS:
            for pt in series[mn][hz]:
                ts = pt["ts"]
                if pt["winrate"] is not None:
                    sparkline_by_ts.setdefault(ts, []).append(pt["winrate"])

        sparkline = [
            {"ts": ts, "winrate": round(float(np.mean(wrs)), 4)}
            for ts, wrs in sorted(sparkline_by_ts.items())
            if wrs
        ]

        # Suffisance statistique sur la fenêtre récente
        total_n_recent = sum(trends[hz]["n_now"] for hz in _HORIZONS)

        models_out[mn] = {
            "timeseries":       series[mn],
            "trend":            trends,
            "global_trend":     global_dir,
            "sparkline":        sparkline,
            "insufficient_data": total_n_recent < 30,
        }

    first_ts = (hist_meta["first_ts"] or now) if hist_meta else now
    hours_of_history = (now - first_ts) / 3600

    return {
        "models": models_out,
        "meta": {
            "days_requested":     days,
            "hours_of_history":   round(hours_of_history, 1),
            "has_24h_history":    hours_of_history >= 24,
            "has_48h_history":    hours_of_history >= 48,
            "total_snapshots":    hist_meta["n"] if hist_meta else 0,
        },
    }


def get_arena_stats(days: int = 30) -> dict:
    """Rapport complet arena pour l'endpoint /api/model_arena."""
    all_models = [
        _EXPERT_NAME, _EXPERT2_NAME, _NAIVE_NAME, _AUTOCAL_NAME, _ML_NAME,
        _ACRS_NAME, _AME_NAME, _NEURAL_TAB_NAME, _TEMPORAL_NAME, _MDE_NAME,
        _BME_NAME,
    ]
    performance, current_preds, statuses = {}, {}, {}

    expert_perf = _model_stats(_EXPERT_NAME, days=days)

    for mn in all_models:
        performance[mn]   = _model_stats(mn, days=days)
        current_preds[mn] = _current_predictions(mn)
        total_n = sum(
            s.get("n_signals", 0)
            for s in performance[mn].values()
            if isinstance(s, dict)
        )
        if mn == _ACRS_NAME:
            statuses[mn] = "shadow"
        elif mn == _AME_NAME:
            statuses[mn] = "experimental_memory"
        elif mn == _NEURAL_TAB_NAME:
            statuses[mn] = "experimental_neural"
        elif mn == _TEMPORAL_NAME:
            statuses[mn] = "experimental_temporal"
        elif mn == _MDE_NAME:
            statuses[mn] = "baseline_smart" if total_n >= _MIN_OUTCOMES_COLLECTING else "collecting"
        elif mn == _BME_NAME:
            statuses[mn] = "challenger_momentum" if total_n >= _MIN_OUTCOMES_PROMOTION else "warming_up"
        elif mn in [_AUTOCAL_NAME, _ML_NAME]:
            # Legacy statuses for old engines
            if mn == _ML_NAME:
                statuses[mn] = "challenger" if total_n >= _MIN_OUTCOMES_PROMOTION else "expérimental"
            else:
                statuses[mn] = "challenger" if total_n >= _MIN_OUTCOMES_PROMOTION else "observation"
        else:
            statuses[mn] = _arena_status(mn, total_n, performance[mn], expert_perf)

    best = _best_model(days=days)

    with _conn() as c:
        row_n  = c.execute("SELECT COUNT(*) as n FROM model_outcomes").fetchone()
        row_ts = c.execute("SELECT MIN(timestamp) as ts FROM model_predictions").fetchone()
        total_outcomes = row_n["n"] if row_n else 0
        first_ts = row_ts["ts"] if row_ts and row_ts["ts"] else int(time.time())

    days_of_data = (int(time.time()) - first_ts) / 86400

    # Résumé par moteur primaire
    primary_summary = {}
    for mn in _PRIMARY_MODELS:
        perf = performance[mn]
        n_total = sum(s.get("n_signals", 0) for s in perf.values() if isinstance(s, dict))
        wrs = [s["winrate"] for s in perf.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        evs = [s["ev_mean"]  for s in perf.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        primary_summary[mn] = {
            "n_evaluated": n_total,
            "avg_winrate": round(sum(wrs) / len(wrs), 3) if wrs else None,
            "avg_ev": round(sum(evs) / len(evs), 3) if evs else None,
            "status": statuses[mn],
            "collecting": n_total < _MIN_OUTCOMES_COLLECTING,
        }

    return {
        "current_predictions": current_preds,
        "performance": performance,
        "best_model": best,
        "best_primary_model": (
            max(
                _PRIMARY_MODELS,
                key=lambda mn: primary_summary[mn].get("avg_winrate") or -1,
            )
            if any(primary_summary[mn]["avg_winrate"] for mn in _PRIMARY_MODELS)
            else _EXPERT_NAME
        ),
        "principal_engine": _EXPERT_NAME,
        "primary_models": _PRIMARY_MODELS,
        "model_statuses": statuses,
        "primary_summary": primary_summary,
        "promotion_criteria": {
            **{mn: _promotion_check(mn) for mn in [_EXPERT2_NAME, _AUTOCAL_NAME, _ML_NAME]},
            _AME_NAME: _ame_promotion_check(),
        },
        "weight_multipliers": {
            mn: {hz: _get_weight_multipliers(mn, hz) for hz in _HORIZONS}
            for mn in [_AUTOCAL_NAME]
        },
        "meta": {
            "days_analyzed": days,
            "total_outcomes_db": total_outcomes,
            "days_of_data": round(days_of_data, 1),
            "min_outcomes_for_promotion": _MIN_OUTCOMES_PROMOTION,
            "min_days_for_promotion": _MIN_DAYS_PROMOTION,
            "min_outcomes_collecting": _MIN_OUTCOMES_COLLECTING,
        },
    }


def get_mde_vs_naive_comparison(days: int = 30) -> dict:
    """Comparaison head-to-head MOPI Divergence Engine vs Naive Baseline.

    Mesure si les divergences MOPI battent réellement le Naive en WR, EV et Profit Factor.
    Applique les règles de sécurité (EXPLORATION / SIGNAL FRAGILE / SIGNAL ROBUSTE).
    """
    from .mopi_divergence_engine import (
        compute_unique_setup_count, get_setup_label,
        _N_EXPLORATION, _N_FRAGILE,
    )

    mde_stats   = _model_stats(_MDE_NAME,   days=days)
    naive_stats = _model_stats(_NAIVE_NAME, days=days)

    # Setup label basé sur le comptage unique setups horizon 4h
    setup_info = compute_unique_setup_count(_MDE_NAME, "4h", days=days)
    n_unique   = setup_info["unique_setup_count"]
    setup_label = get_setup_label(n_unique)
    can_promote = n_unique >= _N_EXPLORATION

    horizon_results: Dict[str, dict] = {}
    horizons_with_data: List[str] = []

    for hz in _HORIZONS:
        mde_h   = mde_stats.get(hz, {})
        naive_h = naive_stats.get(hz, {})

        mde_n   = mde_h.get("n_signals", 0)
        naive_n = naive_h.get("n_signals", 0)

        if mde_n == 0 or naive_n == 0:
            horizon_results[hz] = {
                "status":  "no_data",
                "mde_n":   mde_n,
                "naive_n": naive_n,
            }
            continue

        mde_wr  = mde_h.get("winrate", 0.0) or 0.0
        mde_ev  = mde_h.get("ev_mean", 0.0) or 0.0
        mde_pf  = mde_h.get("profit_factor")

        naive_wr = naive_h.get("winrate", 0.0) or 0.0
        naive_ev = naive_h.get("ev_mean", 0.0) or 0.0
        naive_pf = naive_h.get("profit_factor")

        delta_wr = round(mde_wr - naive_wr, 3)
        delta_ev = round(mde_ev - naive_ev, 4)
        delta_pf = round((mde_pf or 0.0) - (naive_pf or 0.0), 3) if mde_pf is not None and naive_pf is not None else None

        # Critères de victoire absolus
        mde_beats_wr = mde_wr  > naive_wr
        mde_beats_ev = mde_ev  > max(0.0, naive_ev)
        mde_beats_pf = (mde_pf or 0.0) > max(1.0, (naive_pf or 0.0))

        score = sum([mde_beats_wr, mde_beats_ev, mde_beats_pf])
        if score == 3:
            verdict_hz = "MDE_WINS"
        elif score == 2:
            verdict_hz = "MDE_AHEAD"
        elif score == 1:
            verdict_hz = "MDE_BEHIND"
        else:
            verdict_hz = "NAIVE_WINS"

        horizons_with_data.append(hz)
        horizon_results[hz] = {
            "mde": {
                "n":             mde_n,
                "winrate":       mde_wr,
                "ev":            mde_ev,
                "profit_factor": mde_pf,
                "avg_win":       mde_h.get("avg_win"),
                "avg_loss":      mde_h.get("avg_loss"),
                "is_noisy":      mde_h.get("is_noisy", True),
            },
            "naive": {
                "n":             naive_n,
                "winrate":       naive_wr,
                "ev":            naive_ev,
                "profit_factor": naive_pf,
                "avg_win":       naive_h.get("avg_win"),
                "avg_loss":      naive_h.get("avg_loss"),
            },
            "delta": {
                "winrate":       delta_wr,
                "ev":            delta_ev,
                "profit_factor": delta_pf,
            },
            "mde_beats": {
                "wr":    mde_beats_wr,
                "ev":    mde_beats_ev,
                "pf":    mde_beats_pf,
                "score": f"{score}/3",
            },
            "verdict":  verdict_hz,
            "is_noisy": mde_n < _N_EXPLORATION or mde_h.get("is_noisy", True),
        }

    # Verdict global
    verdicts  = [horizon_results[hz].get("verdict") for hz in horizons_with_data]
    wins_count = sum(1 for v in verdicts if v in ("MDE_WINS", "MDE_AHEAD"))

    if not horizons_with_data:
        global_verdict  = "INSUFFICIENT_DATA"
        global_message  = "Aucune donnée — pas encore d'outcomes enregistrés pour MDE ou Naive."
    elif not can_promote:
        global_verdict  = "EXPLORATION"
        global_message  = (
            f"EXPLORATION — {n_unique} setups uniques "
            f"(min {_N_EXPLORATION} requis). Signal non validé, ne pas promouvoir."
        )
    elif wins_count >= 2:
        global_verdict  = "MDE_BEATS_NAIVE"
        global_message  = (
            f"MDE bat le Naive Baseline sur {wins_count}/{len(horizons_with_data)} horizons — "
            "signal statistiquement supérieur."
        )
    else:
        global_verdict  = "NAIVE_STILL_BETTER"
        global_message  = (
            f"Naive Baseline tient encore — MDE devant sur {wins_count}/{len(horizons_with_data)} horizons seulement."
        )

    # Règles de promotion MDE → baseline officielle
    promotion_ready = (
        can_promote
        and wins_count >= 2
        and all(
            horizon_results.get(hz, {}).get("mde", {}).get("n", 0) >= _MDE_MIN_OUTCOMES_PROMOTION
            for hz in horizons_with_data
        )
    )

    return {
        "setup_label":       setup_label,
        "n_unique_setups":   n_unique,
        "can_promote":       can_promote,
        "promotion_ready":   promotion_ready,
        "global_verdict":    global_verdict,
        "global_message":    global_message,
        "horizons":          horizon_results,
        "horizons_with_data": horizons_with_data,
        "days_analyzed":     days,
        "thresholds": {
            "exploration_lt":         _N_EXPLORATION,       # N < 30 → EXPLORATION
            "signal_fragile_range":   f"{_N_EXPLORATION}-{_N_FRAGILE - 1}",
            "signal_robuste_gte":     _N_FRAGILE,           # N >= 100 → SIGNAL ROBUSTE
            "outcomes_promotion_min": _MDE_MIN_OUTCOMES_PROMOTION,
            "winrate_promotion_min":  _MIN_WINRATE_PROMOTION,
        },
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def get_arena_history(hours: int = 24) -> dict:
    """Timeline des prédictions et outcomes pour /api/model_arena/history."""
    cutoff = int(time.time()) - hours * 3600
    models_in = tuple(_PRIMARY_MODELS)
    placeholders = ",".join("?" * len(models_in))

    with _conn() as c:
        pred_rows = c.execute(
            f"""SELECT id, timestamp, model_name, model_version, horizon,
                       spot_at_prediction, prob_up, prob_down, prob_range,
                       confidence, dominant_scenario, created_at
                FROM model_predictions
                WHERE timestamp >= ? AND model_name IN ({placeholders})
                ORDER BY timestamp DESC LIMIT 300""",
            (cutoff, *models_in),
        ).fetchall()

        outcome_rows = c.execute(
            f"""SELECT mo.prediction_id, mo.horizon, mo.spot_entry, mo.spot_exit,
                       mo.return_pct, mo.realized_direction, mo.is_correct,
                       mo.evaluated_at, mp.model_name, mp.model_version
                FROM model_outcomes mo
                JOIN model_predictions mp ON mo.prediction_id = mp.id
                WHERE mo.evaluated_at >= ? AND mp.model_name IN ({placeholders})
                ORDER BY mo.evaluated_at DESC LIMIT 200""",
            (cutoff, *models_in),
        ).fetchall()

    timeline_preds = [dict(r) for r in pred_rows]
    timeline_outcomes = [dict(r) for r in outcome_rows]

    # Winrate cumulatif par heure par moteur
    wr_by_model: Dict[str, list] = {mn: [] for mn in _PRIMARY_MODELS}
    for r in outcome_rows:
        mn = r["model_name"]
        if mn in wr_by_model:
            ts = r["evaluated_at"] // 3600 * 3600
            wr_by_model[mn].append({"ts": ts, "correct": r["is_correct"]})

    winrate_timeline = {}
    for mn, events in wr_by_model.items():
        by_hour: Dict[int, dict] = {}
        running_hit = running_n = 0
        for ev in sorted(events, key=lambda x: x["ts"]):
            running_n   += 1
            running_hit += ev["correct"]
            h = ev["ts"]
            by_hour[h] = {
                "ts": h,
                "winrate": round(running_hit / running_n, 3),
                "n": running_n,
            }
        winrate_timeline[mn] = sorted(by_hour.values(), key=lambda x: x["ts"])

    return {
        "hours": hours,
        "timeline_predictions": timeline_preds[:100],
        "timeline_outcomes": timeline_outcomes[:100],
        "winrate_timeline": winrate_timeline,
    }


def get_arena_health() -> dict:
    """Audit complet santé Arena — traçabilité données live/seed, workers, progression."""
    now = int(time.time())

    with _conn() as c:
        total_preds   = c.execute("SELECT COUNT(*) FROM model_predictions").fetchone()[0]
        total_outcomes = c.execute("SELECT COUNT(*) FROM model_outcomes").fetchone()[0]

        # Seed vs live
        try:
            seed_preds = c.execute(
                "SELECT COUNT(*) FROM model_predictions WHERE is_seed=1"
            ).fetchone()[0]
            seed_outcomes = c.execute(
                """SELECT COUNT(*) FROM model_outcomes mo
                   JOIN model_predictions mp ON mo.prediction_id = mp.id
                   WHERE mp.is_seed=1"""
            ).fetchone()[0]
        except Exception:
            seed_preds = seed_outcomes = 0

        live_preds    = total_preds - seed_preds
        live_outcomes = total_outcomes - seed_outcomes

        # Timestamps
        last_pred_ts = (
            c.execute(
                "SELECT MAX(created_at) FROM model_predictions WHERE is_seed=0 OR is_seed IS NULL"
            ).fetchone()[0]
        )
        last_outcome_ts = c.execute("SELECT MAX(evaluated_at) FROM model_outcomes").fetchone()[0]

        # Pending outcomes
        pending_total = c.execute(
            """SELECT COUNT(*) FROM model_predictions mp
               LEFT JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mo.prediction_id IS NULL AND mp.timestamp <= ?""",
            (now - 4 * 3600,),
        ).fetchone()[0]

        # SQL audit — par moteur
        pred_by_model = {
            row[0]: row[1]
            for row in c.execute(
                "SELECT model_name, COUNT(*) FROM model_predictions GROUP BY model_name"
            ).fetchall()
        }
        outcome_by_model = {
            row[0]: row[1]
            for row in c.execute(
                """SELECT mp.model_name, COUNT(*)
                   FROM model_outcomes mo
                   JOIN model_predictions mp ON mo.prediction_id = mp.id
                   GROUP BY mp.model_name"""
            ).fetchall()
        }
        pending_by_model = {
            row[0]: row[1]
            for row in c.execute(
                """SELECT mp.model_name, COUNT(*)
                   FROM model_predictions mp
                   LEFT JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mo.prediction_id IS NULL AND mp.timestamp <= ?
                   GROUP BY mp.model_name""",
                (now - 4 * 3600,),
            ).fetchall()
        }

        # Premier timestamp global
        first_ts_row = c.execute("SELECT MIN(timestamp) FROM model_predictions").fetchone()
        first_ts = first_ts_row[0] if first_ts_row and first_ts_row[0] else now

        # Prochaine évaluation due (oldest pending 4h)
        oldest_4h = c.execute(
            """SELECT MIN(mp.timestamp) FROM model_predictions mp
               LEFT JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mp.horizon = '4h' AND mo.prediction_id IS NULL""",
        ).fetchone()

    days_total = (now - first_ts) / 86400
    workers_ok = bool(last_pred_ts and (now - last_pred_ts) < 7200)

    next_eval_ts = None
    if oldest_4h and oldest_4h[0]:
        next_eval_ts = oldest_4h[0] + 4 * 3600

    # ── Per-model leaderboard detail ──────────────────────────────────────────
    all_models = [_EXPERT_NAME, _EXPERT2_NAME, _AUTOCAL_NAME, _ML_NAME, _NAIVE_NAME, _ACRS_NAME, _AME_NAME, _MDE_NAME]
    leaderboard_detail: Dict[str, dict] = {}

    for mn in all_models:
        stats = _model_stats(mn, days=30)

        with _conn() as c:
            fts_row = c.execute(
                "SELECT MIN(timestamp) FROM model_predictions WHERE model_name=?", (mn,)
            ).fetchone()
        fts = fts_row[0] if fts_row and fts_row[0] else now
        days_hist = round((now - fts) / 86400, 1)

        n_live_out  = outcome_by_model.get(mn, 0)
        n_pending_m = pending_by_model.get(mn, 0)

        wrs  = [s["winrate"]  for s in stats.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        evs  = [s["ev_mean"]  for s in stats.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        maes = [s["mae"]      for s in stats.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        mfes = [s["mfe"]      for s in stats.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]

        if mn == _AME_NAME:
            promo = _ame_promotion_check()
        elif mn not in (_EXPERT_NAME, _NAIVE_NAME):
            promo = _promotion_check(mn)
        else:
            promo = None
        if promo:
            crit = promo.get("criteria", {})
            keys = list(crit.keys())
            met  = sum(1 for k in keys if crit[k])
            promo_pct = round(met / len(keys) * 100) if keys else 0
        else:
            crit      = {}
            promo_pct = 100 if mn == _EXPERT_NAME else 0

        leaderboard_detail[mn] = {
            "model":              mn,
            "live_outcomes":      n_live_out,
            "pending_outcomes":   n_pending_m,
            "winrate":            round(sum(wrs) / len(wrs), 3) if wrs else None,
            "ev":                 round(sum(evs) / len(evs), 3) if evs else None,
            "mae":                round(sum(maes) / len(maes), 3) if maes else None,
            "mfe":                round(sum(mfes) / len(mfes), 3) if mfes else None,
            "days_of_history":    days_hist,
            "promotion_eligible": promo.get("promotion_ready", False) if promo else (mn == _EXPERT_NAME),
            "promotion_pct":      promo_pct,
            "promotion_criteria": crit,
        }

    # ── Progression best model ────────────────────────────────────────────────
    winner = _best_model(days=30)
    winner_detail = leaderboard_detail.get(winner, {})
    winner_pct    = winner_detail.get("promotion_pct", 0)
    winner_crit   = winner_detail.get("promotion_criteria", {})

    # ── Trust gate ────────────────────────────────────────────────────────────
    trust_gate = {
        "min_14_days":        days_total >= _MIN_DAYS_PROMOTION,
        "min_100_outcomes":   live_outcomes >= _MIN_OUTCOMES_PROMOTION,
        "min_500_outcomes":   live_outcomes >= 500,
        "days_done":          round(days_total, 1),
        "winner_label": (
            "Moteur gagnant"   if (days_total >= _MIN_DAYS_PROMOTION and live_outcomes >= _MIN_OUTCOMES_PROMOTION)
            else "Leader provisoire" if live_outcomes >= _MIN_OUTCOMES_COLLECTING
            else "Collecte en cours"
        ),
    }

    # ── Significativité statistique estimée ───────────────────────────────────
    outcomes_per_day = round(live_outcomes / max(1, days_total), 1) if days_total > 0 else 0
    days_to_100  = round(max(0, _MIN_OUTCOMES_PROMOTION - live_outcomes) / max(1, outcomes_per_day), 0) if outcomes_per_day > 0 else None
    days_to_500  = round(max(0, 500 - live_outcomes) / max(1, outcomes_per_day), 0) if outcomes_per_day > 0 else None

    # ── Statut global ─────────────────────────────────────────────────────────
    if total_preds == 0:
        arena_status = "empty"
    elif live_outcomes < _MIN_OUTCOMES_COLLECTING:
        arena_status = "collecting"
    else:
        arena_status = "active"

    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    return {
        # Résumé
        "arena_status":          arena_status,
        "workers_running":       workers_ok,
        "workers_last_pred_ago_min": round((now - last_pred_ts) / 60, 1) if last_pred_ts else None,
        # Compteurs data
        "live_predictions":      live_preds,
        "live_outcomes":         live_outcomes,
        "seed_predictions":      seed_preds,
        "seed_outcomes":         seed_outcomes,
        "total_predictions":     total_preds,
        "total_outcomes":        total_outcomes,
        "pending_outcomes":      pending_total,
        "real_ratio":            round(live_preds / total_preds, 3) if total_preds > 0 else 0.0,
        "leaderboard_uses":      "live_only" if seed_preds == 0 else "seed_present_warning",
        # Timestamps
        "last_prediction_ts":    _iso(last_pred_ts),
        "last_outcome_ts":       _iso(last_outcome_ts),
        "next_evaluation_due":   _iso(next_eval_ts),
        "days_of_data":          round(days_total, 1),
        # SQL audit
        "sql_audit": {
            "predictions_total":   total_preds,
            "outcomes_total":      total_outcomes,
            "predictions_by_model": pred_by_model,
            "outcomes_by_model":    outcome_by_model,
            "first_prediction_ts":  _iso(first_ts),
            "last_prediction_ts":   _iso(last_pred_ts),
            "last_outcome_ts":      _iso(last_outcome_ts),
        },
        # Leaderboard par moteur
        "leaderboard_detail":    leaderboard_detail,
        # Progression
        "progression": {
            "winner_model":    winner,
            "winner_pct":      winner_pct,
            "winner_criteria": winner_crit,
        },
        # Trust gate
        "trust_gate":            trust_gate,
        # Estimation significativité
        "significance_estimate": {
            "current_live_outcomes": live_outcomes,
            "outcomes_per_day":      outcomes_per_day,
            "days_to_100_outcomes":  days_to_100,
            "days_to_500_outcomes":  days_to_500,
            "min_significance_date": _iso(
                int(now + (days_to_100 or 0) * 86400)
            ) if days_to_100 is not None else None,
        },
        "health_ts": datetime.now(timezone.utc).isoformat(),
    }


def get_multi_metric_leaderboard(days: int = 30) -> dict:
    """Leaderboard multi-métriques — 6 phases d'audit complet.

    Phase 1 : 4 classements globaux (WR / EV / PF / Sharpe)
    Phase 2 : classements par horizon (4h / 24h / 72h)
    Phase 3 : significativité statistique (Wilson CI + test Z)
    Phase 4 : détection de bruit (N insuffisant, classements instables)
    Phase 5 : leaders distincts par métrique
    Phase 6 : conclusion automatique
    """
    all_models = [_EXPERT_NAME, _EXPERT2_NAME, _AUTOCAL_NAME, _ML_NAME, _NAIVE_NAME, _ACRS_NAME, _AME_NAME, _MDE_NAME]
    _MIN_N_SIGNIFICANT = 30

    # ── Collecte de toutes les stats ──────────────────────────────────────────
    model_data: Dict[str, dict] = {}
    for mn in all_models:
        stats = _model_stats(mn, days=days)
        model_data[mn] = stats

    # ── Helper : métriques agrégées toutes horizons ───────────────────────────
    def _aggregate(mn: str) -> dict:
        d = model_data[mn]
        rows_per_hz: Dict[str, list] = {}
        for hz in _HORIZONS:
            s = d.get(hz, {})
            if isinstance(s, dict) and s.get("n_signals", 0) >= 1:
                rows_per_hz[hz] = s

        if not rows_per_hz:
            return {"n_total": 0}

        n_total  = sum(s["n_signals"] for s in rows_per_hz.values())
        wr_list  = [s["winrate"]       for s in rows_per_hz.values()]
        ev_list  = [s["ev_mean"]       for s in rows_per_hz.values()]
        pf_list  = [s["profit_factor"] for s in rows_per_hz.values() if s.get("profit_factor") is not None]
        sh_list  = [s["sharpe"]        for s in rows_per_hz.values() if s.get("sharpe") is not None]
        so_list  = [s["sortino"]       for s in rows_per_hz.values() if s.get("sortino") is not None]
        aw_list  = [s["avg_win"]       for s in rows_per_hz.values()]
        al_list  = [s["avg_loss"]      for s in rows_per_hz.values()]
        dd_list  = [s["max_drawdown_cumulative"] for s in rows_per_hz.values()]
        ci_lo    = [s["ci_lower"]      for s in rows_per_hz.values()]
        ci_hi    = [s["ci_upper"]      for s in rows_per_hz.values()]
        moe_list = [s["margin_of_error"] for s in rows_per_hz.values()]

        avg_wr  = float(np.mean(wr_list))
        avg_ev  = float(np.mean(ev_list))
        avg_pf  = float(np.mean(pf_list))  if pf_list else None
        avg_sh  = float(np.mean(sh_list))  if sh_list else None
        avg_so  = float(np.mean(so_list))  if so_list else None
        avg_aw  = float(np.mean(aw_list))
        avg_al  = float(np.mean(al_list))
        max_dd  = float(np.max(dd_list))   if dd_list else None
        avg_moe = float(np.mean(moe_list)) if moe_list else None

        # Intervalle de confiance global agrégé
        ci_lo_g = float(np.mean(ci_lo))
        ci_hi_g = float(np.mean(ci_hi))

        return {
            "n_total":     n_total,
            "winrate":     round(avg_wr, 3),
            "ev":          round(avg_ev, 4),
            "profit_factor": round(avg_pf, 3) if avg_pf is not None else None,
            "sharpe":      round(avg_sh, 3)   if avg_sh is not None else None,
            "sortino":     round(avg_so, 3)   if avg_so is not None else None,
            "avg_win":     round(avg_aw, 4),
            "avg_loss":    round(avg_al, 4),
            "max_drawdown": round(max_dd, 4)  if max_dd is not None else None,
            "ci_lower":    round(ci_lo_g, 4),
            "ci_upper":    round(ci_hi_g, 4),
            "margin_of_error": round(avg_moe, 4) if avg_moe is not None else None,
            "is_sufficient_n": n_total >= _MIN_N_SIGNIFICANT,
            "by_horizon":  {
                hz: {k: v for k, v in d[hz].items() if k != "status"}
                for hz in _HORIZONS
                if isinstance(d.get(hz), dict) and d[hz].get("n_signals", 0) >= 1
            },
        }

    aggregated = {mn: _aggregate(mn) for mn in all_models}

    # ── Phase 1 : 4 classements globaux ──────────────────────────────────────
    def _rank(metric: str, eligible_only: bool = True) -> list:
        ranked = []
        for mn in all_models:
            agg = aggregated[mn]
            if eligible_only and not agg.get("is_sufficient_n"):
                continue
            val = agg.get(metric)
            if val is None:
                continue
            ranked.append({"model": mn, "value": val, "n": agg["n_total"]})
        ranked.sort(key=lambda x: x["value"], reverse=True)
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
        return ranked

    rank_winrate = _rank("winrate")
    rank_ev      = _rank("ev")
    rank_pf      = _rank("profit_factor")
    rank_sharpe  = _rank("sharpe")

    # ── Phase 2 : classements par horizon ─────────────────────────────────────
    def _rank_horizon(hz: str, metric: str) -> list:
        ranked = []
        for mn in all_models:
            s = model_data[mn].get(hz, {})
            if not isinstance(s, dict) or s.get("n_signals", 0) < _MIN_N_SIGNIFICANT:
                continue
            val = s.get(metric)
            if val is None:
                continue
            ranked.append({"model": mn, "value": val, "n": s["n_signals"]})
        ranked.sort(key=lambda x: x["value"], reverse=True)
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
        return ranked

    by_horizon: Dict[str, dict] = {}
    for hz in _HORIZONS:
        by_horizon[hz] = {
            "winrate":      _rank_horizon(hz, "winrate"),
            "ev":           _rank_horizon(hz, "ev_mean"),
            "profit_factor": _rank_horizon(hz, "profit_factor"),
            "sharpe":       _rank_horizon(hz, "sharpe"),
            "note": "n_min=30 pour inclure un moteur dans ce classement",
        }

    # ── Phase 3 : significativité statistique ─────────────────────────────────
    stat_sig: Dict[str, dict] = {}
    for mn in all_models:
        agg = aggregated[mn]
        if not agg.get("is_sufficient_n"):
            stat_sig[mn] = {"verdict": "N insuffisant", "n": agg.get("n_total", 0)}
            continue
        n   = agg["n_total"]
        wr  = agg["winrate"]
        moe = agg.get("margin_of_error", 0.5)
        ci_lo = agg["ci_lower"]
        ci_hi = agg["ci_upper"]
        # Significatif si l'intervalle de confiance exclut 0.5 (random baseline)
        beats_random = ci_lo > 0.5 or ci_hi < 0.5
        stat_sig[mn] = {
            "n":           n,
            "winrate":     wr,
            "ci_95":       [ci_lo, ci_hi],
            "margin_of_error": moe,
            "beats_random_wr": beats_random,
            "verdict": "Significatif" if beats_random and n >= 30 else "Bruit probable",
        }

    # Tests Z entre paires (leader WR vs les autres)
    if rank_winrate:
        leader_mn = rank_winrate[0]["model"]
        leader_s  = model_data[leader_mn]
        z_tests: Dict[str, dict] = {}
        for mn in all_models:
            if mn == leader_mn:
                continue
            # Agrège sur tous les horizons communs
            k1 = sum(
                model_data[leader_mn][hz].get("n_wins", 0)
                for hz in _HORIZONS if isinstance(model_data[leader_mn].get(hz), dict)
            )
            n1 = sum(
                model_data[leader_mn][hz].get("n_signals", 0)
                for hz in _HORIZONS if isinstance(model_data[leader_mn].get(hz), dict)
            )
            k2 = sum(
                model_data[mn][hz].get("n_wins", 0)
                for hz in _HORIZONS if isinstance(model_data[mn].get(hz), dict)
            )
            n2 = sum(
                model_data[mn][hz].get("n_signals", 0)
                for hz in _HORIZONS if isinstance(model_data[mn].get(hz), dict)
            )
            z_tests[mn] = _z_test_proportions(k1, n1, k2, n2)
        stat_sig["_z_tests_vs_wr_leader"] = {  # type: ignore[assignment]
            "leader": leader_mn,
            "comparisons": z_tests,
        }
    else:
        leader_mn = None

    # ── Phase 4 : détection de bruit ──────────────────────────────────────────
    noise_flags: list = []
    # Classements instables : le n°1 change selon la métrique
    leaders = {
        "winrate":       rank_winrate[0]["model"] if rank_winrate else None,
        "ev":            rank_ev[0]["model"]      if rank_ev      else None,
        "profit_factor": rank_pf[0]["model"]      if rank_pf      else None,
        "sharpe":        rank_sharpe[0]["model"]  if rank_sharpe  else None,
    }
    unique_leaders = len(set(v for v in leaders.values() if v))
    if unique_leaders > 1:
        noise_flags.append({
            "type": "classements_instables",
            "detail": f"{unique_leaders} leaders différents selon la métrique — aucun moteur ne domine clairement",
            "leaders": leaders,
        })

    for mn in all_models:
        agg = aggregated[mn]
        n = agg.get("n_total", 0)
        if n < _MIN_N_SIGNIFICANT:
            noise_flags.append({
                "type": "n_insuffisant",
                "model": mn,
                "n": n,
                "min_required": _MIN_N_SIGNIFICANT,
                "detail": f"{mn} : {n} outcomes < {_MIN_N_SIGNIFICANT} minimum",
            })
        elif n < 100:
            moe = agg.get("margin_of_error", 0)
            if moe and moe > 0.08:
                noise_flags.append({
                    "type": "marge_erreur_elevee",
                    "model": mn,
                    "n": n,
                    "margin_of_error": moe,
                    "detail": f"{mn} : marge d'erreur {moe:.1%} — différences < {moe:.1%} non significatives",
                })

    # Horizons sans N suffisant
    for hz in _HORIZONS:
        hz_models_ok = [mn for mn in all_models if isinstance(model_data[mn].get(hz), dict) and model_data[mn][hz].get("n_signals", 0) >= _MIN_N_SIGNIFICANT]
        if len(hz_models_ok) < 2:
            noise_flags.append({
                "type": "horizon_insuffisant",
                "horizon": hz,
                "models_ok": hz_models_ok,
                "detail": f"Horizon {hz} : moins de 2 moteurs comparables — classement non significatif",
            })

    # ── Phase 5 : leaderboard multi-leaders ───────────────────────────────────
    multi_leaders = {}
    for metric, ranking in [("winrate", rank_winrate), ("ev", rank_ev), ("profit_factor", rank_pf), ("sharpe", rank_sharpe)]:
        if ranking:
            top = ranking[0]
            multi_leaders[metric] = {
                "model": top["model"],
                "value": top["value"],
                "n":     top["n"],
            }
        else:
            multi_leaders[metric] = None

    # Y a-t-il UN seul leader qui gagne sur toutes les métriques ?
    leader_models = [v["model"] for v in multi_leaders.values() if v]
    dominant_leader = leader_models[0] if len(set(leader_models)) == 1 and leader_models else None

    # ── Phase 6 : conclusion automatique ──────────────────────────────────────
    def _best_hz(hz: str) -> Optional[str]:
        ev_r = _rank_horizon(hz, "ev_mean")
        return ev_r[0]["model"] if ev_r else None

    best_4h  = _best_hz("4h")
    best_24h = _best_hz("24h")
    best_72h = _best_hz("72h")
    best_global = multi_leaders.get("ev", {}) or {}
    best_global_mn = best_global.get("model") if isinstance(best_global, dict) else None

    # Peut-on déjà promouvoir ?
    max_n = max((aggregated[mn].get("n_total", 0) for mn in all_models), default=0)
    can_promote = (
        max_n >= _MIN_OUTCOMES_PROMOTION
        and not any(f["type"] == "classements_instables" for f in noise_flags)
    )

    diffs_significant = not any(
        f["type"] in ("classements_instables", "marge_erreur_elevee")
        for f in noise_flags
    )

    conclusion = {
        "best_global_ev":      best_global_mn,
        "best_4h":             best_4h,
        "best_24h":            best_24h,
        "best_72h":            best_72h,
        "dominant_leader":     dominant_leader,
        "diffs_significant":   diffs_significant,
        "can_promote":         can_promote,
        "max_n_outcomes":      max_n,
        "summary": (
            f"Moteur dominant : {dominant_leader} (leader sur toutes métriques)."
            if dominant_leader else
            f"Aucun moteur ne domine sur toutes les métriques. "
            f"Leader EV: {best_global_mn or 'N/A'}, "
            f"Leader WR: {leaders.get('winrate') or 'N/A'}. "
            f"{'Différences statistiquement significatives.' if diffs_significant else 'Différences dans la marge d erreur — collecte insuffisante.'}"
        ),
        "can_promote_reason": (
            "N >= 100 + classement stable → promotion possible"
            if can_promote else
            f"Collecte en cours : max {max_n}/{_MIN_OUTCOMES_PROMOTION} outcomes requis"
            if max_n < _MIN_OUTCOMES_PROMOTION else
            "Classements instables selon la métrique — continuer la collecte"
        ),
    }

    return {
        "days":       days,
        "all_models": all_models,
        "aggregated": {mn: aggregated[mn] for mn in all_models},
        "rankings": {
            "global": {
                "winrate":       rank_winrate,
                "ev":            rank_ev,
                "profit_factor": rank_pf,
                "sharpe":        rank_sharpe,
            },
            "by_horizon": by_horizon,
        },
        "statistical_significance": stat_sig,
        "noise_flags":  noise_flags,
        "multi_leaders": multi_leaders,
        "conclusion":    conclusion,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }


def get_arena_debug() -> dict:
    """Debug info pour /api/model_arena/debug."""
    now = int(time.time())
    horizon_secs = {"4h": 4 * 3600, "24h": 24 * 3600, "72h": 72 * 3600}

    with _conn() as c:
        total_preds = c.execute("SELECT COUNT(*) as n FROM model_predictions").fetchone()["n"]
        total_outcomes = c.execute("SELECT COUNT(*) as n FROM model_outcomes").fetchone()["n"]

        pending = c.execute(
            """SELECT COUNT(*) as n FROM model_predictions mp
               LEFT JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mo.prediction_id IS NULL
                 AND mp.timestamp <= ?""",
            (now - 4 * 3600,),
        ).fetchone()["n"]

        by_model_rows = c.execute(
            """SELECT mp.model_name,
                      COUNT(mp.id) as n_preds,
                      SUM(CASE WHEN mo.prediction_id IS NOT NULL THEN 1 ELSE 0 END) as n_evaluated,
                      MAX(mp.created_at) as last_pred_ts
               FROM model_predictions mp
               LEFT JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               GROUP BY mp.model_name"""
        ).fetchall()

    by_model = {}
    for row in by_model_rows:
        mn = row["model_name"]
        n_preds = row["n_preds"] or 0
        n_ev = row["n_evaluated"] or 0
        last_ts = row["last_pred_ts"]
        by_model[mn] = {
            "total_predictions": n_preds,
            "evaluated": n_ev,
            "pending": n_preds - n_ev,
            "last_prediction_ago_min": round((now - last_ts) / 60, 1) if last_ts else None,
        }

    # Prochain check par horizon
    next_check = {}
    for hz, secs in horizon_secs.items():
        cutoff = now - secs
        with _conn() as c:
            oldest = c.execute(
                """SELECT mp.timestamp FROM model_predictions mp
                   LEFT JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.horizon = ? AND mo.prediction_id IS NULL
                   ORDER BY mp.timestamp ASC LIMIT 1""",
                (hz,),
            ).fetchone()
        if oldest and oldest["timestamp"]:
            expires_in = (oldest["timestamp"] + secs) - now
            next_check[hz] = max(0, round(expires_in / 60, 1))
        else:
            next_check[hz] = None

    return {
        "total_predictions": total_preds,
        "total_outcomes": total_outcomes,
        "pending_evaluation": pending,
        "by_model": by_model,
        "next_outcome_check_min": next_check,
        "debug_ts": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────── Phase 1 — Outcome Audit ─────────────────────────

def get_outcome_audit(limit: int = 50) -> dict:
    """Top N outcomes récents — valider la logique d'évaluation outcome par outcome."""
    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    with _conn() as c:
        rows = c.execute(
            """SELECT
                mp.timestamp         AS prediction_ts,
                mo.evaluated_at      AS evaluation_ts,
                mp.model_name        AS model,
                mp.horizon,
                mo.spot_entry,
                mo.spot_exit,
                mp.dominant_scenario AS predicted_direction,
                mo.realized_direction,
                mo.return_pct,
                mo.is_correct
               FROM model_outcomes mo
               JOIN model_predictions mp ON mo.prediction_id = mp.id
               ORDER BY mo.evaluated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    outcomes = []
    for r in rows:
        outcomes.append({
            "prediction_ts":      _iso(r["prediction_ts"]),
            "evaluation_ts":      _iso(r["evaluation_ts"]),
            "model":              r["model"],
            "horizon":            r["horizon"],
            "spot_entry":         r["spot_entry"],
            "spot_exit":          r["spot_exit"],
            "predicted_direction": r["predicted_direction"],
            "realized_direction": r["realized_direction"],
            "return_pct":         round(r["return_pct"], 4) if r["return_pct"] is not None else None,
            "is_correct":         bool(r["is_correct"]) if r["is_correct"] is not None else None,
        })

    n = len(outcomes)
    n_correct = sum(1 for o in outcomes if o["is_correct"])
    n_up   = sum(1 for o in outcomes if o["realized_direction"] == "UP")
    n_down = sum(1 for o in outcomes if o["realized_direction"] == "DOWN")
    n_range = sum(1 for o in outcomes if o["realized_direction"] == "RANGE")

    return {
        "outcomes": outcomes,
        "meta": {
            "count": n,
            "winrate_sample": round(n_correct / n, 3) if n > 0 else None,
            "direction_distribution": {
                "UP": n_up, "DOWN": n_down, "RANGE": n_range,
            },
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    }


# ─────────────────────────── Phase 2 — Weights Audit ─────────────────────────

def get_weights_audit() -> dict:
    """Audit auto-calibration — poids initiaux vs actuels, preuve que ça bouge."""
    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    with _conn() as c:
        rows = c.execute(
            """SELECT group_name, horizon, weight_multiplier, last_updated, weekly_delta
               FROM arena_weights
               WHERE model_name = ?
               ORDER BY horizon, group_name""",
            (_AUTOCAL_NAME,),
        ).fetchall()

        n_outcomes_row = c.execute(
            """SELECT COUNT(*) as n FROM model_outcomes mo
               JOIN model_predictions mp ON mo.prediction_id = mp.id
               WHERE mp.model_name = ?""",
            (_AUTOCAL_NAME,),
        ).fetchone()

    n_outcomes = n_outcomes_row["n"] if n_outcomes_row else 0

    existing: Dict[tuple, dict] = {}
    for r in rows:
        key = (r["group_name"], r["horizon"])
        existing[key] = {
            "group":        r["group_name"],
            "horizon":      r["horizon"],
            "initial":      1.0,
            "current":      round(float(r["weight_multiplier"]), 4),
            "delta":        round(float(r["weight_multiplier"]) - 1.0, 4),
            "delta_pct":    round((float(r["weight_multiplier"]) - 1.0) * 100, 2),
            "weekly_delta": round(float(r["weekly_delta"] or 0), 4),
            "last_updated": _iso(r["last_updated"]),
        }

    # Groups/horizons not yet in DB = unchanged at 1.0
    table = list(existing.values())
    for hz in _HORIZONS:
        for g in _FACTOR_GROUPS:
            if (g, hz) not in existing:
                table.append({
                    "group": g, "horizon": hz,
                    "initial": 1.0, "current": 1.0,
                    "delta": 0.0, "delta_pct": 0.0,
                    "weekly_delta": 0.0, "last_updated": None,
                })

    table.sort(key=lambda x: abs(x["delta"]), reverse=True)

    # Summary by group (avg across horizons)
    from collections import defaultdict as _dd
    by_group: Dict[str, list] = _dd(list)
    for row in table:
        by_group[row["group"]].append(row)

    group_summary = []
    for g, rows_g in by_group.items():
        avg_curr = sum(r["current"] for r in rows_g) / len(rows_g)
        max_delta = max(abs(r["delta"]) for r in rows_g)
        last_upd  = max(
            (r["last_updated"] for r in rows_g if r["last_updated"]),
            default=None,
        )
        group_summary.append({
            "group":          g,
            "avg_current":    round(avg_curr, 4),
            "avg_delta_pct":  round((avg_curr - 1.0) * 100, 2),
            "max_abs_delta":  round(max_delta, 4),
            "last_updated":   last_upd,
        })
    group_summary.sort(key=lambda x: abs(x["avg_delta_pct"]), reverse=True)

    any_moved = any(abs(r["delta"]) > 0.001 for r in table)

    return {
        "weights_detail":  table,
        "group_summary":   group_summary,
        "meta": {
            "model":                  _AUTOCAL_NAME,
            "n_outcomes_calibrated":  n_outcomes,
            "calibration_active":     any_moved,
            "max_weekly_change_pct":  _MAX_WEIGHT_WEEKLY_CHANGE * 100,
            "weight_bounds":          [_WEIGHT_MIN, _WEIGHT_MAX],
            "ts":                     datetime.now(timezone.utc).isoformat(),
        },
    }


# ─────────────────────────── Shadow Debug ─────────────────────────────────────

def get_shadow_debug() -> dict:
    """Debug détaillé du moteur auto_calibrated_regime_shadow.

    Affiche pour chaque horizon :
      - poids globaux Auto-Calibrated
      - régime actuel
      - poids finaux shadow
      - propositions appliquées (delta réel utilisé)
      - propositions bloquées (N < 30 ou EV indispo)
    """
    engine = AutoCalibratedRegimeShadowEngine()
    by_horizon: Dict[str, dict] = {}
    total_applied = 0
    total_blocked = 0
    current_regimes: List[str] = []

    for hz in _HORIZONS:
        global_mults, shadow_mults, applied, blocked, regimes = engine._compute_shadow_weights(hz)
        if regimes:
            current_regimes = regimes  # identique pour tous les horizons
        total_applied += len(applied)
        total_blocked += len(blocked)
        by_horizon[hz] = {
            "global_weights":  {k: round(v, 4) for k, v in global_mults.items()},
            "final_weights":   {k: round(v, 4) for k, v in shadow_mults.items()},
            "regime_weights": {
                entry["group"]: {
                    "global":  entry["global_weight"],
                    "delta":   entry["delta"],
                    "final":   entry["final_weight"],
                    "engine":  entry["engine"],
                    "n":       entry["n"],
                    "ev":      entry["ev"],
                }
                for entry in applied
            },
            "applied_proposals": applied,
            "blocked_proposals": blocked,
        }

    # Stats du moteur shadow
    with _conn() as c:
        n_preds_row = c.execute(
            "SELECT COUNT(*) as n FROM model_predictions WHERE model_name = ?",
            (_ACRS_NAME,),
        ).fetchone()
        n_outcomes_row = c.execute(
            """SELECT COUNT(*) as n FROM model_outcomes mo
               JOIN model_predictions mp ON mo.prediction_id = mp.id
               WHERE mp.model_name = ?""",
            (_ACRS_NAME,),
        ).fetchone()

    return {
        "model":           _ACRS_NAME,
        "version":         _ACRS_VERSION,
        "current_regimes": current_regimes,
        "n_applied_total": total_applied,
        "n_blocked_total": total_blocked,
        "by_horizon":      by_horizon,
        "stats": {
            "n_predictions": n_preds_row["n"] if n_preds_row else 0,
            "n_outcomes":    n_outcomes_row["n"] if n_outcomes_row else 0,
        },
        "note": "Test apprentissage par régime — non utilisé en production.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────── Phase 3+4 — Feature Audit ───────────────────────

def get_feature_audit(days: int = 30) -> dict:
    """Feature Audit — valeur prédictive par feature ET par horizon. Live only (is_seed=0)."""
    cutoff = int(time.time()) - days * 86400

    with _conn() as c:
        rows = c.execute(
            """SELECT mp.explanation_json, mp.horizon,
                      mp.dominant_scenario      AS predicted_direction,
                      mo.is_correct, mo.return_pct,
                      mo.direction_adjusted_return,
                      mo.realized_direction
               FROM model_predictions mp
               JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mp.model_name = ? AND mp.timestamp >= ?
                 AND mo.realized_direction IS NOT NULL
                 AND mp.is_seed = 0""",
            (_EXPERT_NAME, cutoff),
        ).fetchall()

    def _reliability_label(n: int) -> str:
        if n < 10:  return "Exploration"
        if n < 30:  return "Fragile"
        if n < 100: return "Exploitable"
        return "Robuste"

    # Accumulateurs {(group, horizon): stats}
    group_hz_data: Dict[tuple, dict] = {}

    def _init_slot() -> dict:
        return {
            "hits": 0, "total": 0, "returns": [],
            "dir": {d: {"hits": 0, "total": 0} for d in ["UP", "DOWN", "RANGE"]},
        }

    for r in rows:
        expl_str = r["explanation_json"]
        if not expl_str:
            continue
        try:
            expl = json.loads(expl_str)
        except Exception:
            continue

        rules = expl.get("rules", [])
        active_groups: set = set()
        for rule in rules:
            g   = rule.get("group", "other")
            pts = rule.get("pts_applied", 0)
            if pts != 0 and g in _FACTOR_GROUPS:
                active_groups.add(g)

        hz       = r["horizon"]
        pred_dir = r["predicted_direction"]
        real_dir = r["realized_direction"]

        # direction_adjusted_return: use stored value, fallback for legacy rows
        dir_adj = (
            r["direction_adjusted_return"]
            if r["direction_adjusted_return"] is not None
            else _direction_adjusted(r["return_pct"], pred_dir)
        )

        for g in active_groups:
            key = (g, hz)
            if key not in group_hz_data:
                group_hz_data[key] = _init_slot()
            data = group_hz_data[key]
            data["total"] += 1
            if r["is_correct"] == 1:
                data["hits"] += 1
            if dir_adj is not None:
                data["returns"].append(dir_adj)
            if pred_dir in data["dir"]:
                data["dir"][pred_dir]["total"] += 1
                if pred_dir == real_dir:
                    data["dir"][pred_dir]["hits"] += 1

    results = []
    for hz in _HORIZONS:
        for g in _FACTOR_GROUPS:
            data = group_hz_data.get((g, hz), _init_slot())
            n       = data["total"]
            hits    = data["hits"]
            returns = data["returns"]

            winrate    = round(hits / n, 3) if n > 0 else None
            ev         = round(float(np.mean(returns)), 3) if returns else None
            reliability = _reliability_label(n)

            # Score exige N >= 30 (Exploitable+)
            if n >= 30 and winrate is not None:
                base     = max(0.0, (winrate - 0.33) / 0.67) * 100
                n_conf   = min(1.0, n / 100.0)
                ev_bonus = min(10.0, max(-10.0, (ev or 0.0) * 3.0))
                score    = max(0, min(100, round(base * n_conf + ev_bonus)))
            else:
                score = None

            dir_breakdown: Dict[str, dict] = {}
            for d in ["UP", "DOWN", "RANGE"]:
                dtot = data["dir"][d]["total"]
                dhit = data["dir"][d]["hits"]
                dir_breakdown[d] = {
                    "n":        dtot,
                    "accuracy": round(dhit / dtot, 3) if dtot > 0 else None,
                }

            results.append({
                "feature":             g,
                "horizon":             hz,
                "sample_size":         n,
                "reliability":         reliability,
                "winrate":             winrate,
                "ev":                  ev,
                "avg_return":          ev,
                "predictive_score":    score,
                "direction_breakdown": dir_breakdown,
            })

    results.sort(key=lambda x: (x["predictive_score"] is not None, x["predictive_score"] or 0), reverse=True)

    top_features    = [r for r in results if r["sample_size"] >= 30 and (r["predictive_score"] or 0) >= 60]
    bottom_features = [r for r in results if r["sample_size"] >= 30 and (r["predictive_score"] or 0) < 45]
    n_insufficient  = [r for r in results if r["sample_size"] < 30]
    anomalies       = [r for r in results if r["sample_size"] < 30 and r["winrate"] is not None and r["winrate"] >= 0.70]

    top_by_horizon: Dict[str, list] = {}
    bottom_by_horizon: Dict[str, list] = {}
    for hz in _HORIZONS:
        hz_rows = [r for r in results if r["horizon"] == hz and r["sample_size"] >= 30]
        hz_sorted = sorted(hz_rows, key=lambda x: x["predictive_score"] or 0, reverse=True)
        top_by_horizon[hz]    = hz_sorted[:3]
        bottom_by_horizon[hz] = [r for r in hz_sorted if (r["predictive_score"] or 0) < 45][:3]

    return {
        "features":           results,
        "top_features":       top_features[:6],
        "bottom_features":    list(reversed(bottom_features[-6:])),
        "n_insufficient":     n_insufficient,
        "top_by_horizon":     top_by_horizon,
        "bottom_by_horizon":  bottom_by_horizon,
        "anomalies":          anomalies,
        "meta": {
            "model":                   _EXPERT_NAME,
            "days":                    days,
            "live_only":               True,
            "total_outcomes_analyzed": len(rows),
            "ts":                      datetime.now(timezone.utc).isoformat(),
        },
    }


# ─────────────────────────── Phase 5 — Combination Audit ─────────────────────

def get_feature_combination_audit(days: int = 30) -> dict:
    """Test combinaisons de features — quelles paires apportent vraiment du signal."""
    from itertools import combinations as _comb
    from collections import defaultdict as _dd

    cutoff = int(time.time()) - days * 86400

    with _conn() as c:
        rows = c.execute(
            """SELECT mp.explanation_json, mp.dominant_scenario,
                      mo.is_correct, mo.return_pct,
                      mo.direction_adjusted_return
               FROM model_predictions mp
               JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mp.model_name = ? AND mp.timestamp >= ?
                 AND mo.realized_direction IS NOT NULL""",
            (_EXPERT_NAME, cutoff),
        ).fetchall()

    combo_data: dict = _dd(lambda: {"hits": 0, "total": 0, "returns": []})

    for r in rows:
        expl_str = r["explanation_json"]
        if not expl_str:
            continue
        try:
            expl = json.loads(expl_str)
        except Exception:
            continue

        rules = expl.get("rules", [])
        active_groups: set = set()
        for rule in rules:
            g   = rule.get("group", "other")
            pts = rule.get("pts_applied", 0)
            if pts != 0 and g in _FACTOR_GROUPS:
                active_groups.add(g)

        dir_adj = (
            r["direction_adjusted_return"]
            if r["direction_adjusted_return"] is not None
            else _direction_adjusted(r["return_pct"], r["dominant_scenario"])
        )

        for g1, g2 in _comb(sorted(active_groups), 2):
            key = f"{g1}+{g2}"
            combo_data[key]["total"] += 1
            if r["is_correct"] == 1:
                combo_data[key]["hits"] += 1
            if dir_adj is not None:
                combo_data[key]["returns"].append(dir_adj)

    results = []
    for combo, data in combo_data.items():
        n       = data["total"]
        hits    = data["hits"]
        returns = data["returns"]
        if n < 5:
            continue
        winrate       = round(hits / n, 3)
        ev            = round(float(np.mean(returns)), 3) if returns else None
        median_return = round(float(np.median(returns)), 3) if returns else None

        results.append({
            "combination":    combo,
            "n":              n,
            "winrate":        winrate,
            "ev":             ev,
            "median_return":  median_return,
        })

    results.sort(key=lambda x: x["winrate"], reverse=True)

    return {
        "combinations":  results,
        "top_winning":   results[:10],
        "top_losing":    list(reversed(results[-10:])) if len(results) > 10 else [],
        "meta": {
            "days":       days,
            "min_sample": 5,
            "ts":         datetime.now(timezone.utc).isoformat(),
        },
    }


# ─────────────────────────── Phase 6 — Feature Health ────────────────────────

def get_feature_health() -> dict:
    """Feature Health — couverture, fraîcheur, qualité de chaque feature."""
    now = int(time.time())

    with _conn() as c:
        rows = c.execute(
            """SELECT features_json, explanation_json, timestamp
               FROM model_predictions
               WHERE model_name = ?
               ORDER BY timestamp DESC LIMIT 500""",
            (_EXPERT_NAME,),
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {"features": [], "meta": {"error": "no_data", "ts": datetime.now(timezone.utc).isoformat()}}

    group_stats: Dict[str, dict] = {
        g: {"present": 0, "active": 0, "last_ts": 0}
        for g in _FACTOR_GROUPS
    }

    for r in rows:
        ts = r["timestamp"] or 0

        feat_str = r["features_json"]
        feat: dict = {}
        if feat_str:
            try:
                feat = json.loads(feat_str)
            except Exception:
                pass

        expl_str = r["explanation_json"]
        active_groups: set = set()
        if expl_str:
            try:
                expl = json.loads(expl_str)
                for rule in expl.get("rules", []):
                    g   = rule.get("group", "other")
                    pts = rule.get("pts_applied", 0)
                    if pts != 0 and g in group_stats:
                        active_groups.add(g)
            except Exception:
                pass

        for g in _FACTOR_GROUPS:
            keys    = _FEATURE_KEYS.get(g, [])
            present = any(k in feat and feat[k] is not None for k in keys)
            if present:
                group_stats[g]["present"] += 1
                if ts > group_stats[g]["last_ts"]:
                    group_stats[g]["last_ts"] = ts
            if g in active_groups:
                group_stats[g]["active"] += 1

    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    results = []
    for g in _FACTOR_GROUPS:
        stats     = group_stats[g]
        coverage  = round(stats["present"] / total, 3) if total > 0 else 0.0
        last_ts   = stats["last_ts"]
        stale_h   = (now - last_ts) / 3600 if last_ts > 0 else 9999.0
        freshness = round(max(0.0, min(1.0, 1.0 - stale_h / 48.0)), 3)

        stale  = stale_h > 6
        usable = coverage >= 0.50 and not stale

        quality_score = round((coverage * 0.6 + freshness * 0.4) * 100)

        results.append({
            "feature":         g,
            "coverage":        coverage,
            "freshness":       freshness,
            "quality_score":   quality_score,
            "stale":           stale,
            "usable":          usable,
            "last_seen":       _iso(last_ts) if last_ts > 0 else None,
            "staleness_hours": round(stale_h, 1) if stale_h < 9999 else None,
        })

    results.sort(key=lambda x: x["quality_score"], reverse=True)

    n_usable = sum(1 for r in results if r["usable"])
    n_stale  = sum(1 for r in results if r["stale"])

    return {
        "features": results,
        "meta": {
            "predictions_analyzed": total,
            "n_usable":             n_usable,
            "n_stale":              n_stale,
            "ts":                   datetime.now(timezone.utc).isoformat(),
        },
    }


# ─────────────────────────── Neural Observability Helpers ────────────────────

def _iso_ts(ts) -> Optional[str]:
    """Convert unix timestamp to ISO UTC string."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _count_outcomes_for_engine(hz: str) -> int:
    """Count labeled outcomes (expert model, non-seed) available for neural training."""
    with _conn() as c:
        row = c.execute(
            """SELECT COUNT(*) FROM model_predictions mp
               JOIN model_outcomes mo
                 ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
               WHERE mp.model_name = ? AND mp.horizon = ?
                 AND mo.realized_direction IS NOT NULL AND mp.is_seed = 0""",
            (_EXPERT_NAME, hz),
        ).fetchone()
    return int(row[0]) if row else 0


def _log_retrain_attempt(
    model_name: str, hz: str, status: str,
    n_outcomes: int, duration_s: float, error_msg: str = None,
) -> None:
    """Log a retrain attempt to neural_retrain_attempts."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO neural_retrain_attempts
                   (model_name, horizon, attempted_at, status, error_msg, n_outcomes, duration_s)
                   VALUES (?,?,?,?,?,?,?)""",
                (model_name, hz, int(time.time()), status, error_msg, n_outcomes, round(duration_s, 3)),
            )
            c.commit()
    except Exception:
        pass


def _compute_neural_eta(current: int, target: int, hz: str) -> Optional[str]:
    """Estimate time to reach target outcomes based on 7-day rolling rate."""
    if current >= target:
        return None
    missing = target - current
    week_ago = int(time.time()) - 7 * 86400
    with _conn() as c:
        row = c.execute(
            """SELECT COUNT(*) FROM model_outcomes mo
               JOIN model_predictions mp ON mp.id = mo.prediction_id AND mp.horizon = mo.horizon
               WHERE mp.model_name = ? AND mp.horizon = ?
                 AND mo.realized_direction IS NOT NULL AND mp.is_seed = 0
                 AND mo.evaluated_at > ?""",
            (_EXPERT_NAME, hz, week_ago),
        ).fetchone()
    n_recent = int(row[0]) if row else 0
    if n_recent == 0:
        return "inconnu"
    days = missing / (n_recent / 7.0)
    if days < 1:
        return f"~{int(days * 24)}h"
    elif days < 30:
        return f"~{int(days)}j"
    else:
        return f"~{days / 30:.1f} mois"


# ─────────────────────────── Neural Debug Functions ──────────────────────────

def get_neural_tabular_debug() -> dict:
    """Debug info pour /api/model_arena/neural_tabular_debug."""
    ts = datetime.now(timezone.utc).isoformat()
    horizons_info = {}

    for hz in _HORIZONS:
        meta_path = Path(os.path.join(_NEURAL_MODEL_DIR, f"tabular_{hz}_meta.json"))
        meta: dict = {}
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass

        n = meta.get("n_training_samples", 0)
        if n < _NEURAL_TAB_WARMUP:
            status = "WARMING_UP"
        elif n < _NEURAL_TAB_SHADOW:
            status = "SHADOW"
        else:
            status = meta.get("model_status", "UNKNOWN")

        horizons_info[hz] = {
            "status":              status,
            "n_training_samples":  n,
            "last_trained":        _iso_ts(meta.get("trained_at", 0)) if meta.get("trained_at") else None,
            "val_winrate":         meta.get("val_winrate"),
            "val_n":               meta.get("val_n"),
            "confusion_matrix":    meta.get("confusion_matrix"),
            "top_features":        meta.get("top_features"),
            "fi_scores":           meta.get("fi_scores", [])[:10],
            "model_file_exists":   Path(os.path.join(_NEURAL_MODEL_DIR, f"tabular_{hz}.npz")).exists(),
        }

    with _conn() as c:
        log_rows = c.execute(
            """SELECT model_name, horizon, trained_at, n_samples, val_winrate, notes
               FROM neural_training_log WHERE model_name = ?
               ORDER BY trained_at DESC LIMIT 20""",
            (_NEURAL_TAB_NAME,),
        ).fetchall()

    training_history = [
        {
            "horizon":     r["horizon"],
            "trained_at":  _iso_ts(r["trained_at"]),
            "n_samples":   r["n_samples"],
            "val_winrate": r["val_winrate"],
            "notes":       r["notes"],
        }
        for r in log_rows
    ]

    return {
        "model_name":       _NEURAL_TAB_NAME,
        "version":          _NEURAL_TAB_VERSION,
        "architecture":     "MLP D=14 → 32 → 16 → 3 (softmax)",
        "thresholds":       {
            "warmup":         _NEURAL_TAB_WARMUP,
            "shadow":         _NEURAL_TAB_SHADOW,
            "conf_cap_low":   _NEURAL_CONF_CAP_LOW,
            "conf_cap_high":  _NEURAL_CONF_CAP_HIGH,
            "conf_max":       _NEURAL_CONF_MAX,
        },
        "horizons":         horizons_info,
        "training_history": training_history,
        "model_dir":        _NEURAL_MODEL_DIR,
        "ts":               ts,
    }


def get_temporal_neural_debug() -> dict:
    """Debug info pour /api/model_arena/temporal_neural_debug."""
    ts = datetime.now(timezone.utc).isoformat()
    horizons_info = {}

    for hz in _HORIZONS:
        meta_path = Path(os.path.join(_NEURAL_MODEL_DIR, f"temporal_{hz}_meta.json"))
        meta: dict = {}
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass

        n = meta.get("n_training_samples", 0)
        if n < _TEMPORAL_WARMUP:
            status = "WARMING_UP"
        elif n < _TEMPORAL_SHADOW:
            status = "SHADOW"
        else:
            status = meta.get("model_status", "UNKNOWN")

        horizons_info[hz] = {
            "status":             status,
            "n_sequences":        n,
            "seq_len":            meta.get("seq_len", _NEURAL_SEQ_LEN),
            "last_trained":       _iso_ts(meta.get("trained_at", 0)) if meta.get("trained_at") else None,
            "val_winrate":        meta.get("val_winrate"),
            "val_n":              meta.get("val_n"),
            "confusion_matrix":   meta.get("confusion_matrix"),
            "model_file_exists":  Path(os.path.join(_NEURAL_MODEL_DIR, f"temporal_{hz}.npz")).exists(),
        }

    with _conn() as c:
        log_rows = c.execute(
            """SELECT model_name, horizon, trained_at, n_samples, val_winrate, notes
               FROM neural_training_log WHERE model_name = ?
               ORDER BY trained_at DESC LIMIT 20""",
            (_TEMPORAL_NAME,),
        ).fetchall()

    training_history = [
        {
            "horizon":     r["horizon"],
            "trained_at":  _iso_ts(r["trained_at"]),
            "n_samples":   r["n_samples"],
            "val_winrate": r["val_winrate"],
            "notes":       r["notes"],
        }
        for r in log_rows
    ]

    return {
        "model_name":       _TEMPORAL_NAME,
        "version":          _TEMPORAL_VERSION,
        "architecture":     f"GRU D={_TEMPORAL_FEATURES} hidden=32 → 3 (softmax), seq_len={_NEURAL_SEQ_LEN}",
        "thresholds":       {
            "warmup":        _TEMPORAL_WARMUP,
            "shadow":        _TEMPORAL_SHADOW,
            "conf_cap_low":  _TEMPORAL_CONF_CAP_LOW,
            "conf_cap":      _TEMPORAL_CONF_CAP,
        },
        "horizons":         horizons_info,
        "training_history": training_history,
        "model_dir":        _NEURAL_MODEL_DIR,
        "ts":               ts,
    }


# ─────────────────────────── Phases 2/3/5 — Neural Observability ─────────────

def get_neural_health() -> dict:
    """Phase 2 — Preuve de vie : statut complet, outcomes réels, alertes."""
    ts  = datetime.now(timezone.utc).isoformat()
    now = int(time.time())

    def _engine_block(eng: str, warmup: int, prefix: str) -> dict:
        horizons: dict = {}
        for hz in _HORIZONS:
            n_outcomes = _count_outcomes_for_engine(hz)
            n_display  = max(0, n_outcomes - (_NEURAL_SEQ_LEN - 1)) if eng == _TEMPORAL_NAME else n_outcomes

            meta: dict = {}
            mp = Path(os.path.join(_NEURAL_MODEL_DIR, f"{prefix}_{hz}_meta.json"))
            if mp.exists():
                try:
                    with open(mp) as fh:
                        meta = json.load(fh)
                except Exception:
                    pass

            trained_at = meta.get("trained_at", 0)
            val_wr     = meta.get("val_winrate")
            val_ev     = meta.get("val_ev")
            val_pf     = meta.get("val_pf")
            model_file = Path(os.path.join(_NEURAL_MODEL_DIR, f"{prefix}_{hz}.npz")).exists()

            with _conn() as c:
                training_count = c.execute(
                    "SELECT COUNT(*) FROM neural_training_log WHERE model_name=? AND horizon=?",
                    (eng, hz),
                ).fetchone()[0]
                last_att = c.execute(
                    """SELECT attempted_at, status FROM neural_retrain_attempts
                       WHERE model_name=? AND horizon=? ORDER BY attempted_at DESC LIMIT 1""",
                    (eng, hz),
                ).fetchone()
                last_pred_ts = c.execute(
                    "SELECT MAX(timestamp) FROM model_predictions WHERE model_name=? AND horizon=?",
                    (eng, hz),
                ).fetchone()[0]

            last_att_ts     = last_att["attempted_at"] if last_att else None
            last_att_status = last_att["status"]       if last_att else None

            # Determine status
            if training_count == 0:
                status = "WARMING_UP"
            elif meta.get("model_status") == "INVALID" or (val_wr is not None and val_wr < 0.45):
                status = "TRAINED_INVALID"
            elif model_file and val_wr is not None and val_wr >= 0.45:
                status = "ACTIVE" if (val_ev is not None and val_ev > 0) else "TRAINED"
            else:
                status = "TRAINED" if model_file else "WARMING_UP"

            # Gate display (Phase 6)
            if training_count < 1:
                display = "WARMING_UP"
            elif val_wr is None or val_wr < 0.45:
                display = "TRAINED_BUT_INVALID"
            elif val_ev is not None and val_ev <= 0:
                display = "TRAINED_BUT_INVALID"
            else:
                display = "NEURAL_ACTIVE"

            prog = round(min(100.0, n_display / warmup * 100), 1) if warmup > 0 else 0.0
            eta  = _compute_neural_eta(n_display, warmup, hz) if n_display < warmup else None

            horizons[hz] = {
                "outcomes_current":          n_display,
                "outcomes_target":           warmup,
                "outcomes_missing":          max(0, warmup - n_display),
                "progression_pct":           prog,
                "eta":                       eta,
                "training_count":            int(training_count),
                "last_retrain_attempt":      _iso_ts(last_att_ts),
                "last_retrain_attempt_status": last_att_status,
                "last_retrain_success":      _iso_ts(trained_at) if trained_at else None,
                "last_prediction":           _iso_ts(last_pred_ts),
                "status":                    status,
                "display_status":            display,
                "val_winrate":               val_wr,
                "val_ev":                    val_ev,
                "val_pf":                    val_pf,
                "model_file_exists":         model_file,
            }
        return horizons

    tab  = _engine_block(_NEURAL_TAB_NAME, _NEURAL_TAB_WARMUP, "tabular")
    temp = _engine_block(_TEMPORAL_NAME,   _TEMPORAL_WARMUP,   "temporal")

    # Phase 4 — Alert detection
    alerts: list = []
    for eng, hz_data, warmup in [
        (_NEURAL_TAB_NAME, tab,  _NEURAL_TAB_WARMUP),
        (_TEMPORAL_NAME,   temp, _TEMPORAL_WARMUP),
    ]:
        for hz, info in hz_data.items():
            n_oc   = info["outcomes_current"]
            n_atm  = info["training_count"]
            lat    = info["last_retrain_attempt"]

            if n_oc >= warmup:
                if lat is None:
                    alerts.append({"engine": eng, "horizon": hz, "type": "NEVER_ATTEMPTED",
                        "msg": f"{eng} {hz}: {n_oc} outcomes — retrain jamais tenté", "level": "red"})
                else:
                    try:
                        from datetime import datetime as _dt
                        age_h = (datetime.now(timezone.utc) -
                                 _dt.fromisoformat(lat.replace("Z", "+00:00"))).total_seconds() / 3600
                        if age_h > _NEURAL_RETRAIN_H + 2:
                            alerts.append({"engine": eng, "horizon": hz, "type": "STALE_ATTEMPT",
                                "msg": f"{eng} {hz}: dernier essai il y a {age_h:.1f}h", "level": "yellow"})
                    except Exception:
                        pass

            if n_oc >= warmup and n_atm == 0:
                alerts.append({"engine": eng, "horizon": hz, "type": "DATA_BUT_UNTRAINED",
                    "msg": f"{eng} {hz}: {n_oc}>={warmup} outcomes mais jamais entraîné", "level": "red"})

            with _conn() as c:
                n_fail = c.execute(
                    """SELECT COUNT(*) FROM neural_retrain_attempts
                       WHERE model_name=? AND horizon=? AND status='failed' AND attempted_at>?""",
                    (eng, hz, now - 24 * 3600),
                ).fetchone()[0]
            if n_fail > 0:
                alerts.append({"engine": eng, "horizon": hz, "type": "RETRAIN_ERRORS",
                    "msg": f"{eng} {hz}: {n_fail} échec(s) retrain en 24h", "level": "red"})

            if n_atm > 0 and info["last_prediction"] is None:
                alerts.append({"engine": eng, "horizon": hz, "type": "TRAINED_NEVER_USED",
                    "msg": f"{eng} {hz}: entraîné mais jamais utilisé en prédiction", "level": "yellow"})

    return {
        "ts": ts,
        "neural_tabular": {
            "model_name":    _NEURAL_TAB_NAME,
            "architecture":  f"MLP D={_NEURAL_TAB_FEATURES} → 32 → 16 → 3",
            "warmup_target": _NEURAL_TAB_WARMUP,
            "shadow_target": _NEURAL_TAB_SHADOW,
            "horizons":      tab,
        },
        "temporal_neural": {
            "model_name":    _TEMPORAL_NAME,
            "architecture":  f"GRU D={_TEMPORAL_FEATURES} hidden=32 seq={_NEURAL_SEQ_LEN} → 3",
            "warmup_target": _TEMPORAL_WARMUP,
            "shadow_target": _TEMPORAL_SHADOW,
            "horizons":      temp,
        },
        "alerts":     alerts,
        "has_alerts": len(alerts) > 0,
    }


def get_neural_training_log(limit: int = 50) -> dict:
    """Phase 3 — Journal complet des retrains avec métriques de validation."""
    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    with _conn() as c:
        rows = c.execute(
            """SELECT model_name, horizon, trained_at, n_samples,
                      val_winrate, val_ev, val_pf, duration_s, status, notes
               FROM neural_training_log ORDER BY trained_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    entries = []
    for r in rows:
        wr = r["val_winrate"]; ev = r["val_ev"]; pf = r["val_pf"]
        entries.append({
            "date":       _iso(r["trained_at"]),
            "engine":     r["model_name"],
            "horizon":    r["horizon"],
            "status":     r["status"] or "success",
            "n_samples":  r["n_samples"],
            "val_winrate": round(wr * 100, 1)  if wr is not None else None,
            "val_ev":      round(ev, 4)         if ev is not None else None,
            "val_pf":      pf,
            "duration_s":  r["duration_s"],
            "notes":       r["notes"],
        })
    return {"ts": datetime.now(timezone.utc).isoformat(), "entries": entries, "count": len(entries)}


def get_neural_learning_curve() -> dict:
    """Phase 5 — Courbe d'apprentissage : n_outcomes → WR/EV/PF de validation."""
    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    curves: dict = {}
    for eng in [_NEURAL_TAB_NAME, _TEMPORAL_NAME]:
        per_hz: dict = {}
        for hz in _HORIZONS:
            with _conn() as c:
                rows = c.execute(
                    """SELECT trained_at, n_samples, val_winrate, val_ev, val_pf
                       FROM neural_training_log WHERE model_name=? AND horizon=?
                       ORDER BY trained_at ASC""",
                    (eng, hz),
                ).fetchall()
            points = []
            for r in rows:
                if r["n_samples"] is None:
                    continue
                wr = r["val_winrate"]; ev = r["val_ev"]; pf = r["val_pf"]
                points.append({
                    "ts":     _iso(r["trained_at"]),
                    "n":      r["n_samples"],
                    "wr_pct": round(wr * 100, 1) if wr is not None else None,
                    "ev_pct": round(ev, 4)        if ev is not None else None,
                    "pf":     pf,
                })
            per_hz[hz] = points
        curves[eng] = per_hz
    return {"ts": datetime.now(timezone.utc).isoformat(), "curves": curves}


def get_confusion_matrix(days: int = 30) -> dict:
    """Matrice de confusion par moteur × horizon.

    Diagnostique les prédictions inversées (pred=UP → réalité=DOWN systématiquement).
    Inclut WR directionnel et si le contrarian mode devrait être actif.
    """
    cutoff = int(time.time()) - days * 86400
    all_models = [
        _EXPERT_NAME, _EXPERT2_NAME, _AUTOCAL_NAME, _ML_NAME, _NAIVE_NAME,
        _ACRS_NAME, _AME_NAME, _NEURAL_TAB_NAME, _TEMPORAL_NAME, _MDE_NAME,
    ]
    result: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "days": days, "engines": {}}

    for model_name in all_models:
        engine_data: dict = {}
        for hz in _HORIZONS:
            with _conn() as c:
                rows = c.execute(
                    """SELECT mp.dominant_scenario as pred, mo.realized_direction as real, COUNT(*) as n
                       FROM model_predictions mp
                       JOIN model_outcomes mo ON mp.id=mo.prediction_id AND mo.horizon=mp.horizon
                       WHERE mp.model_name=? AND mp.horizon=? AND mp.timestamp>=?
                         AND mo.realized_direction IS NOT NULL AND mp.is_seed=0
                       GROUP BY pred, real""",
                    (model_name, hz, cutoff),
                ).fetchall()

            if not rows:
                continue

            matrix: dict = {}
            n_total = n_correct = 0
            for r in rows:
                pred, real, n = r["pred"], r["real"], r["n"]
                matrix.setdefault(pred, {})[real] = n
                n_total += n
                if pred == real:
                    n_correct += n

            # Directional WR (hors RANGE)
            dir_correct = dir_total = 0
            for pred, real_counts in matrix.items():
                if pred not in ("UP", "DOWN"):
                    continue
                for real, n in real_counts.items():
                    if real not in ("UP", "DOWN"):
                        continue
                    dir_total += n
                    if pred == real:
                        dir_correct += n

            dir_wr = round(dir_correct / dir_total, 3) if dir_total >= 10 else None
            contrarian_recommended = dir_wr is not None and dir_total >= 50 and dir_wr < 0.33

            engine_data[hz] = {
                "n_total":   n_total,
                "winrate":   round(n_correct / n_total, 3) if n_total else None,
                "dir_n":     dir_total,
                "dir_winrate": dir_wr,
                "contrarian_recommended": contrarian_recommended,
                "matrix":    matrix,
            }

        if engine_data:
            result["engines"][model_name] = engine_data

    return result
