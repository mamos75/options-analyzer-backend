"""
BTC Momentum Engine (BME) — Logistic Regression Softmax pure numpy.

Insight audit :
  - naive_baseline 61.4% WR 24h → momentum BTC = feature #1
  - auto_calibrated_regime_shadow 63.9% WR 72h → régime GEX = feature #2
  - neural_tabular 19.2% WR 24h → mode contrarian auto requis

Features (22 dimensions) :
  Prix   : momentum 4h / 8h / 24h / 48h, vol réalisée 24h, RSI-14, BB position, mom acceleration
  Options: GEX near, GEX regime, DEX, IV rank, PCR near, MOPI, flip dist, funding, OI, vol, MOPI div
  Composite: momentum × regime, iv × vol_réalisée, max pain distance

Algorithme :
  - Softmax Logistic Regression (3 classes : UP / DOWN / RANGE)
  - Optimiseur Adam, régularisation L2
  - Normalisation robuste (médiane / IQR)
  - Poids de classe : compensent déséquilibre UP>>DOWN>>RANGE
  - Retrain automatique toutes les 24h (N ≥ 100 outcomes)
  - Mode contrarian auto : Wilson 95% IC upper < 0.50 sur N ≥ 30 → inversion UP↔DOWN
  - Segmentation régime : poids softmax différents AMPLIFICATEUR vs STABILISANT
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

from .wilson_utils import contrarian_significant, has_edge as _has_edge, wilson_lower

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

DB_PATH        = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")
MODEL_DIR      = os.environ.get("NEURAL_MODEL_DIR", "/data/neural_models")

_BME_NAME      = "btc_momentum_engine"
_BME_VERSION   = "bme-v1.0"
_HORIZONS      = ["4h", "24h", "72h"]

_RETRAIN_H     = 24      # retrain max toutes les 24h
_MIN_TRAIN     = 100     # N minimum d'outcomes pour entraîner
_N_FEATURES    = 22      # dimension du vecteur de features
_ADAM_LR       = 0.005
_L2_REG        = 0.05
_EPOCHS        = 600
# _CONTRARIAN_THRESH supprimé : remplacé par test Wilson (contrarian_significant)
# Voir wilson_utils.py — condition : wilson_upper(wr, n) < 0.50 avec n >= 30

# Deltas de temps pour le momentum (secondes)
_T_4H  = 4  * 3600
_T_8H  = 8  * 3600
_T_24H = 24 * 3600
_T_48H = 48 * 3600
_T_72H = 72 * 3600

_LABEL_IDX = {"UP": 0, "DOWN": 1, "RANGE": 2}
_IDX_LABEL = {0: "UP", 1: "DOWN", 2: "RANGE"}


# ─── DB helper ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ─── Spot price history cache ─────────────────────────────────────────────────

_spot_cache: List[Tuple[int, float]] = []   # (timestamp, spot)
_spot_cache_ts: float = 0.0
_SPOT_CACHE_TTL = 120  # secondes


def _load_spot_history(hours_back: int = 80) -> List[Tuple[int, float]]:
    """Charge les spots BTC depuis model_predictions (toutes 30min env)."""
    global _spot_cache, _spot_cache_ts
    now = time.time()
    if now - _spot_cache_ts < _SPOT_CACHE_TTL and _spot_cache:
        return _spot_cache
    cutoff = int(now) - hours_back * 3600
    with _conn() as c:
        rows = c.execute(
            """SELECT timestamp, spot_at_prediction
               FROM model_predictions
               WHERE timestamp >= ? AND spot_at_prediction > 0
               GROUP BY timestamp
               ORDER BY timestamp ASC""",
            (cutoff,),
        ).fetchall()
    result = [(r["timestamp"], float(r["spot_at_prediction"])) for r in rows]
    if result:
        _spot_cache = result
        _spot_cache_ts = now
    return result


def _spot_at(spots: List[Tuple[int, float]], target_ts: int) -> Optional[float]:
    """Spot BTC le plus proche du target_ts (fenêtre ±45 min)."""
    if not spots:
        return None
    best_ts, best_val = None, None
    best_delta = float("inf")
    for ts, val in spots:
        d = abs(ts - target_ts)
        if d < best_delta:
            best_delta = d
            best_ts, best_val = ts, val
    if best_delta > 2700:   # > 45 min → trop loin
        return None
    return best_val


def _compute_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """RSI approché sur une liste de prix (retours consécutifs)."""
    if len(prices) < period + 1:
        return None
    returns = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(0, r) for r in returns[-period:]]
    losses = [max(0, -r) for r in returns[-period:]]
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l < 1e-8:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


# ─── Feature extraction ───────────────────────────────────────────────────────

def extract_btc_features(
    spot: float,
    ts: int,
    spots: List[Tuple[int, float]],
) -> Dict[str, Optional[float]]:
    """Calcule les features de prix BTC depuis l'historique."""
    spot_4h  = _spot_at(spots, ts - _T_4H)
    spot_8h  = _spot_at(spots, ts - _T_8H)
    spot_24h = _spot_at(spots, ts - _T_24H)
    spot_48h = _spot_at(spots, ts - _T_48H)

    def ret(prev): return (spot - prev) / prev if prev and prev > 0 else None

    mom_4h  = ret(spot_4h)
    mom_8h  = ret(spot_8h)
    mom_24h = ret(spot_24h)
    mom_48h = ret(spot_48h)

    # Momentum accélération : est-ce que le momentum récent > 2×(momentum mi-chemin) ?
    mom_accel = None
    if mom_4h is not None and mom_8h is not None:
        mom_accel = mom_4h - mom_8h / 2.0   # positif = accélération haussière

    # Volatilité réalisée 24h (std des retours 30min sur les 48 derniers snapshots)
    recent = sorted([(t, v) for t, v in spots if ts - _T_24H <= t <= ts], key=lambda x: x[0])
    vol_24h = None
    if len(recent) >= 4:
        prices_r = [v for _, v in recent]
        rets_r = [(prices_r[i] - prices_r[i-1]) / prices_r[i-1]
                  for i in range(1, len(prices_r)) if prices_r[i-1] > 0]
        if rets_r:
            vol_24h = float(np.std(rets_r))

    # RSI-14 sur spots récents 8h
    recent_8h = sorted([(t, v) for t, v in spots if ts - _T_8H <= t <= ts], key=lambda x: x[0])
    rsi = _compute_rsi([v for _, v in recent_8h], period=14) if len(recent_8h) >= 15 else None

    # Bollinger Band position (spot vs bande dans fenêtre 24h)
    bb_pct = None
    if len(recent) >= 8:
        prices_bb = [v for _, v in recent]
        ma = np.mean(prices_bb)
        std_bb = np.std(prices_bb)
        if std_bb > 0:
            bb_pct = (spot - ma) / (2.0 * std_bb)   # -1=bas bande, +1=haut bande

    return {
        "mom_4h":    mom_4h,
        "mom_8h":    mom_8h,
        "mom_24h":   mom_24h,
        "mom_48h":   mom_48h,
        "mom_accel": mom_accel,
        "vol_24h":   vol_24h,
        "rsi_14":    rsi,
        "bb_pct":    bb_pct,
    }


def build_feature_vector(
    btc_feats: Dict[str, Optional[float]],
    opt_feats: Dict,
) -> Optional[np.ndarray]:
    """Construit le vecteur de features complet (22 dim, borné)."""
    try:
        # ── Prix BTC ─────────────────────────────────────────────────────────
        mom_4h  = float(btc_feats.get("mom_4h")  or 0.0)
        mom_8h  = float(btc_feats.get("mom_8h")  or 0.0)
        mom_24h = float(btc_feats.get("mom_24h") or 0.0)
        mom_48h = float(btc_feats.get("mom_48h") or 0.0)
        mom_acc = float(btc_feats.get("mom_accel") or 0.0)
        vol_24h = float(btc_feats.get("vol_24h") or 0.005)
        rsi     = float(btc_feats.get("rsi_14") or 50.0)
        bb      = float(btc_feats.get("bb_pct")  or 0.0)

        # ── Options ──────────────────────────────────────────────────────────
        gex_near  = float(opt_feats.get("gex_near", 0) or 0)
        dex       = str(opt_feats.get("dex_direction", "") or "")
        iv_rank   = float(opt_feats.get("iv_rank", 50) or 50)
        pcr       = float(opt_feats.get("pc_ratio_near", 1.0) or 1.0)
        mopi      = float(opt_feats.get("mopi_score", 50) or 50)
        flip_d    = float(opt_feats.get("flip_distance_pct", 0) or 0)
        funding   = float(opt_feats.get("funding_rate", 0) or 0)
        oi        = float(opt_feats.get("futures_oi", 0) or 0)
        vol_spot  = float(opt_feats.get("spot_volume_24h", 0) or 0)
        regime    = str(opt_feats.get("gex_regime", "NEUTRE") or "NEUTRE")
        mp_strike = float(opt_feats.get("max_pain_strike", 0) or 0)
        div_type  = float(opt_feats.get("mopi_div_type_enc", 0.0) or 0.0)
        div_str   = float(opt_feats.get("mopi_div_strength", 0.0) or 0.0)

        # ── Encodages ────────────────────────────────────────────────────────
        regime_enc = 1.0 if regime == "STABILISANT" else (-1.0 if regime == "AMPLIFICATEUR" else 0.0)
        dex_enc    = 1.0 if "BULLISH" in dex else (-1.0 if "BEARISH" in dex else 0.0)
        mp_dist    = (mp_strike / float(opt_feats.get("spot", mp_strike or 1)) - 1.0
                      if mp_strike > 0 else 0.0)

        # ── Normalisation / bornage ──────────────────────────────────────────
        v_mom_4h   = np.clip(mom_4h  / 0.05, -3.0, 3.0)
        v_mom_8h   = np.clip(mom_8h  / 0.08, -3.0, 3.0)
        v_mom_24h  = np.clip(mom_24h / 0.12, -3.0, 3.0)
        v_mom_48h  = np.clip(mom_48h / 0.15, -3.0, 3.0)
        v_acc      = np.clip(mom_acc / 0.03, -3.0, 3.0)
        v_vol      = np.clip(vol_24h / 0.01, 0.0, 5.0)      # 0 = calme, 5 = très vol
        v_rsi      = (rsi - 50.0) / 50.0                    # -1=oversold, +1=overbought
        v_bb       = np.clip(bb, -2.0, 2.0)

        v_gex      = np.clip(gex_near / 5e9, -2.0, 2.0)
        v_dex      = dex_enc
        v_iv       = (iv_rank - 50.0) / 50.0
        v_pcr      = np.clip(pcr - 1.0, -1.5, 1.5)
        v_mopi     = (mopi - 50.0) / 50.0
        v_flip     = np.clip(flip_d, -0.15, 0.15) / 0.15
        v_fund     = np.clip(funding / 0.005, -3.0, 3.0)
        v_oi       = np.clip(np.log1p(oi / 1e9) - 2.0, -2.0, 2.0) if oi > 0 else 0.0
        v_vol_s    = np.clip(np.log1p(vol_spot / 1e9) - 3.5, -2.0, 2.0) if vol_spot > 0 else 0.0
        v_mp_dist  = np.clip(mp_dist, -0.05, 0.05) / 0.05

        # ── Features composites ───────────────────────────────────────────────
        v_mom_regime = float(v_mom_24h * regime_enc)     # momentum × régime
        v_iv_vol     = float(v_iv * v_vol / 5.0)         # iv × volatilité réalisée

        vec = np.array([
            v_mom_4h, v_mom_8h, v_mom_24h, v_mom_48h, v_acc,
            v_vol, v_rsi, v_bb,
            v_gex, v_dex, v_iv, v_pcr, v_mopi, v_flip,
            v_fund, v_oi, v_vol_s, v_mp_dist,
            float(np.clip(div_type, -1.0, 1.0)),
            float(np.clip(div_str / 0.03, 0.0, 1.0)),
            v_mom_regime,
            v_iv_vol,
        ], dtype=np.float64)

        # Remplacement NaN/Inf par 0
        vec = np.nan_to_num(vec, nan=0.0, posinf=3.0, neginf=-3.0)
        return vec

    except Exception as e:
        log.warning(f"[bme] build_feature_vector error: {e}")
        return None


# ─── Softmax Logistic Regression (pure numpy) ─────────────────────────────────

def _softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return e / (e.sum(axis=-1, keepdims=True) + 1e-12)


class _SoftmaxModel:
    """Logistic regression multi-classe avec Adam et L2."""

    def __init__(self, n_feat: int = _N_FEATURES, n_cls: int = 3):
        self.W = np.zeros((n_cls, n_feat))
        self.b = np.zeros(n_cls)
        # Adam state
        self._mW = np.zeros_like(self.W)
        self._vW = np.zeros_like(self.W)
        self._mb = np.zeros_like(self.b)
        self._vb = np.zeros_like(self.b)
        self._t  = 0

    def forward(self, X: np.ndarray) -> np.ndarray:
        return _softmax(X @ self.W.T + self.b)

    def _adam_step(self, dW, db, lr, b1=0.9, b2=0.999):
        self._t += 1
        self._mW = b1 * self._mW + (1 - b1) * dW
        self._vW = b2 * self._vW + (1 - b2) * dW ** 2
        self._mb = b1 * self._mb + (1 - b1) * db
        self._vb = b2 * self._vb + (1 - b2) * db ** 2
        mW_hat = self._mW / (1 - b1 ** self._t)
        vW_hat = self._vW / (1 - b2 ** self._t)
        mb_hat = self._mb / (1 - b1 ** self._t)
        vb_hat = self._vb / (1 - b2 ** self._t)
        self.W -= lr * mW_hat / (np.sqrt(vW_hat) + 1e-8)
        self.b -= lr * mb_hat / (np.sqrt(vb_hat) + 1e-8)

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        class_weights: Optional[np.ndarray] = None,
        epochs: int = _EPOCHS,
        lr: float = _ADAM_LR,
        l2: float = _L2_REG,
        batch_size: int = 64,
    ) -> float:
        """Entraîne avec mini-batches. Retourne la cross-entropy finale."""
        n = len(X)
        if class_weights is None:
            class_weights = np.ones(3)
        best_loss = float("inf")
        best_W, best_b = self.W.copy(), self.b.copy()

        for ep in range(epochs):
            idx = np.random.permutation(n)
            for start in range(0, n, batch_size):
                bi = idx[start: start + batch_size]
                Xb, yb = X[bi], y[bi]
                probs = self.forward(Xb)
                # Weighted gradient
                sample_w = class_weights[yb]
                dlogits = probs.copy()
                dlogits[np.arange(len(yb)), yb] -= 1.0
                dlogits *= sample_w[:, None]
                dlogits /= len(yb)
                dW = dlogits.T @ Xb + l2 * self.W
                db = dlogits.sum(axis=0)
                self._adam_step(dW, db, lr)

            if ep % 50 == 0:
                probs_full = self.forward(X)
                ce = -np.mean(class_weights[y] * np.log(probs_full[np.arange(n), y] + 1e-12))
                if ce < best_loss:
                    best_loss = ce
                    best_W, best_b = self.W.copy(), self.b.copy()

        self.W, self.b = best_W, best_b
        return best_loss

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return _softmax(x @ self.W.T + self.b)

    def save(self, path: str):
        np.savez(path, W=self.W, b=self.b)

    def load(self, path: str):
        d = np.load(path + ".npz")
        self.W = d["W"]
        self.b = d["b"]


# ─── Robust scaler ────────────────────────────────────────────────────────────

class _RobustScaler:
    def __init__(self):
        self.med = None
        self.iqr = None

    def fit(self, X: np.ndarray):
        self.med = np.median(X, axis=0)
        q75 = np.percentile(X, 75, axis=0)
        q25 = np.percentile(X, 25, axis=0)
        self.iqr = np.where(q75 - q25 > 1e-8, q75 - q25, 1.0)

    def transform(self, X: np.ndarray) -> np.ndarray:
        return np.clip((X - self.med) / self.iqr, -3.0, 3.0)

    def save(self, path: str):
        np.savez(path, med=self.med, iqr=self.iqr)

    def load(self, path: str):
        d = np.load(path + ".npz")
        self.med = d["med"]
        self.iqr = d["iqr"]


# ─── Main Engine ──────────────────────────────────────────────────────────────

class BTCMomentumEngine:
    """
    Moteur BTC Momentum — Logistic Regression Softmax pure numpy.
    Registré dans model_arena, compatible ArenaOutput.
    """

    name    = _BME_NAME
    version = _BME_VERSION

    # Override contrarian basé sur outcomes prod (mis à jour toutes les heures)
    _PROD_CONTRARIAN_MIN_N  = 15     # N min dir_attempts pour override
    _PROD_CONTRARIAN_THRESH = 0.38   # WR prod < 38% → override contrarian
    _PROD_CONTRARIAN_TTL    = 3600   # re-check toutes les 60 min

    def __init__(self):
        Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)
        self._models: Dict[str, _SoftmaxModel] = {}
        self._scalers: Dict[str, _RobustScaler] = {}
        self._trained_at: Dict[str, int] = {}
        self._contrarian: Dict[str, bool] = {}
        self._n_train: Dict[str, int] = {}
        self._val_wr: Dict[str, float] = {}
        self._prod_contrarian: Dict[str, bool] = {}
        self._prod_contrarian_at: Dict[str, float] = {}
        self._contrarian_decided_at: Dict[str, int] = {}   # B1: ts d'activation contrarian
        self._n_dir_att_at_train: Dict[str, int] = {}      # B1: n_dir_att au moment du train
        for hz in _HORIZONS:
            self._models[hz] = _SoftmaxModel()
            self._scalers[hz] = _RobustScaler()
            self._trained_at[hz] = 0
            self._contrarian[hz] = False
            self._n_train[hz] = 0
            self._val_wr[hz] = 0.0
            self._prod_contrarian[hz] = False
            self._prod_contrarian_at[hz] = 0.0
            self._contrarian_decided_at[hz] = 0
            self._n_dir_att_at_train[hz] = 0
        self._load_all()

    def _model_path(self, hz: str) -> str:
        return os.path.join(MODEL_DIR, f"bme_{hz}_weights")

    def _scaler_path(self, hz: str) -> str:
        return os.path.join(MODEL_DIR, f"bme_{hz}_scaler")

    def _load_all(self):
        for hz in _HORIZONS:
            try:
                mp = self._model_path(hz)
                sp = self._scaler_path(hz)
                mp_path = Path(mp + ".npz")
                sp_path = Path(sp + ".npz")
                if mp_path.exists() and sp_path.exists():
                    self._models[hz].load(mp)
                    self._scalers[hz].load(sp)
                    # Restaure trained_at depuis la date de modification du fichier
                    self._trained_at[hz] = int(mp_path.stat().st_mtime)
                    log.info(f"[bme] {hz} weights loaded (mtime={self._trained_at[hz]})")
            except Exception as e:
                log.warning(f"[bme] load {hz} error: {e}")
        # Vérification immédiate des performances prod au démarrage
        for hz in _HORIZONS:
            self._refresh_prod_contrarian(hz)

    def _get_training_data(
        self, horizon: str
    ) -> Tuple[List[Tuple[int, float, Dict, str]], List[Tuple[int, float]]]:
        """Retourne (rows pour entraînement, spots_history)."""
        with _conn() as c:
            rows = c.execute(
                """SELECT mp.timestamp, mp.spot_at_prediction, mp.features_json,
                          mo.realized_direction
                   FROM model_predictions mp
                   JOIN model_outcomes mo
                     ON mp.id = mo.prediction_id AND mo.horizon = mp.horizon
                   WHERE mp.model_name = 'expert_rules' AND mp.horizon = ?
                     AND mo.realized_direction IS NOT NULL
                     AND mp.spot_at_prediction > 0
                     AND mp.features_json IS NOT NULL
                     AND mp.is_seed = 0
                   ORDER BY mp.timestamp DESC LIMIT 800""",
                (horizon,),
            ).fetchall()

        data = []
        for r in rows:
            try:
                f = json.loads(r["features_json"] or "{}")
                if f:
                    data.append((r["timestamp"], float(r["spot_at_prediction"]), f, r["realized_direction"]))
            except Exception:
                pass

        spots = _load_spot_history(hours_back=80)
        return data, spots

    def _get_binance_training_data(self, horizon: str) -> Tuple[List[np.ndarray], List[int]]:
        """Charge les features/labels Binance pré-calculés pour enrichir le training."""
        try:
            from .bme_binance_enrichment import get_training_data as _bme_enrich_get
            samples = _bme_enrich_get(horizon, max_n=600)
            if samples:
                X = [s[0] for s in samples]
                y = [s[1] for s in samples]
                log.info(f"[bme] {horizon}: {len(X)} samples Binance chargés")
                return X, y
        except Exception as e:
            log.warning(f"[bme] Binance enrichment unavailable: {e}")
        return [], []

    def train(self, horizon: str) -> bool:
        """Entraîne le modèle pour un horizon donné. Retourne True si succès."""
        try:
            t0 = time.time()
            data, spots = self._get_training_data(horizon)
            n = len(data)
            if n < _MIN_TRAIN:
                log.info(f"[bme] {horizon}: {n} samples < {_MIN_TRAIN} min — skip")
                return False

            X_raw, y_raw = [], []
            for ts, spot, opt_feats, label in data:
                btc_f = extract_btc_features(spot, ts, spots)
                opt_feats["spot"] = spot
                vec = build_feature_vector(btc_f, opt_feats)
                if vec is not None and label in _LABEL_IDX:
                    X_raw.append(vec)
                    y_raw.append(_LABEL_IDX[label])

            if len(X_raw) < _MIN_TRAIN:
                log.warning(f"[bme] {horizon}: only {len(X_raw)} valid samples after feature extraction")
                return False

            X_base = np.array(X_raw, dtype=np.float64)
            y_base = np.array(y_raw, dtype=np.int32)

            # Val set = 20% des données INTERNES uniquement (vraies options features)
            # Train set = 80% internes + 100% Binance (enrichissement sans polluer la validation)
            split_internal = int(len(X_base) * 0.8)
            X_tr_int, y_tr_int = X_base[:split_internal], y_base[:split_internal]
            X_val, y_val       = X_base[split_internal:], y_base[split_internal:]

            # Enrichissement Binance dans le train uniquement
            X_binance_raw, y_binance_raw = self._get_binance_training_data(horizon)
            if X_binance_raw:
                X_bin = np.array(X_binance_raw, dtype=np.float64)
                y_bin = np.array(y_binance_raw, dtype=np.int32)
                X_tr = np.vstack([X_tr_int, X_bin])
                y_tr = np.concatenate([y_tr_int, y_bin])
                log.info(f"[bme] {horizon}: train={len(X_tr_int)} int + {len(X_bin)} Binance = {len(X_tr)} | val={len(X_val)} int-only")
            else:
                X_tr, y_tr = X_tr_int, y_tr_int

            # Class weights calculés sur le TRAIN set (inclut Binance pour équilibrer)
            counts = np.bincount(y_tr, minlength=3)
            total = counts.sum()
            class_w = np.where(counts > 0, total / (3.0 * counts + 1e-8), 1.0)
            class_w = np.clip(class_w, 0.5, 10.0)

            # Fit scaler sur train uniquement
            scaler = _RobustScaler()
            scaler.fit(X_tr)
            X_tr_s  = scaler.transform(X_tr)
            X_val_s = scaler.transform(X_val)

            model = _SoftmaxModel()
            model.train(X_tr_s, y_tr, class_weights=class_w)

            # Validation WR directionnel
            val_probs = model.predict_proba(X_val_s)
            val_preds = val_probs.argmax(axis=1)
            dir_mask  = (val_preds != _LABEL_IDX["RANGE"]) & (y_val != _LABEL_IDX["RANGE"])
            dir_wr    = float(np.mean(val_preds[dir_mask] == y_val[dir_mask])) if dir_mask.sum() > 0 else 0.5
            n_dir_att = int(dir_mask.sum())

            # Contrarian mode : Wilson 95% IC — upper < 0.50 signifie WR significativement < chance
            # Remplace l'ancien seuil naïf 0.40 qui ignorait l'incertitude statistique
            contrarian = contrarian_significant(dir_wr, n_dir_att)
            if contrarian:
                from .wilson_utils import wilson_upper as _wu
                _wu_val = _wu(dir_wr, n_dir_att) if n_dir_att > 0 else 1.0
                log.warning(
                    f"[bme] {horizon}: CONTRARIAN MODE actif "
                    f"(WR dir={dir_wr:.1%} wilson_upper={_wu_val:.3f} n={n_dir_att})"
                )

            # Sauvegarde
            self._models[horizon]     = model
            self._scalers[horizon]    = scaler
            self._n_dir_att_at_train[horizon] = n_dir_att  # B1: pour OOS backtest
            prev_contrarian = self._contrarian.get(horizon, False)
            self._contrarian[horizon] = contrarian
            # B1: stocker quand le contrarian a été activé (pour filtrer l'éval OOS)
            if contrarian and not prev_contrarian:
                self._contrarian_decided_at[horizon] = int(time.time())
            elif not contrarian:
                self._contrarian_decided_at[horizon] = 0
            self._trained_at[horizon] = int(time.time())
            self._n_train[horizon]    = len(X_raw)
            self._val_wr[horizon]     = dir_wr

            model.save(self._model_path(horizon))
            scaler.save(self._scaler_path(horizon))

            dur = round(time.time() - t0, 1)
            log.info(
                f"[bme] {horizon} trained: n={len(X_raw)} val_WR={dir_wr:.1%} "
                f"contrarian={contrarian} t={dur}s"
            )

            # Log dans neural_training_log
            with _conn() as c:
                c.execute(
                    """INSERT INTO neural_training_log
                       (model_name, horizon, trained_at, n_samples, val_winrate, notes, duration_s, status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (_BME_NAME, horizon, int(time.time()), len(X_raw), round(dir_wr, 4),
                     f"contrarian={contrarian}", dur, "success"),
                )
                c.commit()
            return True

        except Exception as e:
            log.error(f"[bme] train {horizon} error: {e}", exc_info=True)
            return False

    def _refresh_prod_contrarian(self, horizon: str) -> None:
        """Vérifie les outcomes prod et active le contrarian si WR trop bas.

        Re-check toutes les _PROD_CONTRARIAN_TTL secondes pour ne pas spammer la DB.
        Override immédiat sans attendre le retrain 24h.
        """
        now = time.time()
        if now - self._prod_contrarian_at.get(horizon, 0) < self._PROD_CONTRARIAN_TTL:
            return
        self._prod_contrarian_at[horizon] = now
        try:
            with _conn() as c:
                rows = c.execute(
                    """SELECT mp.dominant_scenario, mo.realized_direction
                       FROM model_predictions mp
                       JOIN model_outcomes mo ON mp.id=mo.prediction_id AND mo.horizon=mp.horizon
                       WHERE mp.model_name=? AND mp.horizon=?
                         AND mp.dominant_scenario IN ('UP','DOWN')
                         AND mo.realized_direction IN ('UP','DOWN')
                         AND mp.is_seed=0
                       ORDER BY mp.created_at DESC LIMIT 100""",
                    (_BME_NAME, horizon),
                ).fetchall()
            if not rows or len(rows) < self._PROD_CONTRARIAN_MIN_N:
                return
            n = len(rows)
            hits = sum(1 for r in rows if r[0] == r[1])
            prod_wr = hits / n
            new_val = prod_wr < self._PROD_CONTRARIAN_THRESH
            if new_val != self._prod_contrarian.get(horizon, False):
                log.warning(
                    f"[bme] {horizon}: prod_contrarian={new_val} "
                    f"(prod_WR={prod_wr:.1%} N={n})"
                )
            self._prod_contrarian[horizon] = new_val
        except Exception as e:
            log.error(f"[bme] _refresh_prod_contrarian {horizon} error: {e}")

    def maybe_retrain(self, horizon: str):
        """Retrain si le modèle n'a pas été entraîné depuis _RETRAIN_H heures."""
        age_h = (time.time() - self._trained_at.get(horizon, 0)) / 3600
        if age_h >= _RETRAIN_H:
            self.train(horizon)

    def predict(self, spot: float, features_snapshot: dict) -> List:
        """Retourne une liste d'ArenaOutput pour les 3 horizons."""
        # Import local pour éviter la dépendance circulaire
        from .model_arena import ArenaOutput, _dominant_from_prob3

        spots = _load_spot_history(hours_back=80)
        ts = int(time.time())
        btc_f = extract_btc_features(spot, ts, spots)
        features_snapshot["spot"] = spot

        results = []
        for hz in _HORIZONS:
            # Retrain si nécessaire (lazy)
            self.maybe_retrain(hz)
            # Override contrarian basé sur performances prod réelles
            self._refresh_prod_contrarian(hz)

            vec = build_feature_vector(btc_f, features_snapshot)
            trained = self._trained_at.get(hz, 0)
            n_tr    = self._n_train.get(hz, 0)

            if vec is None or trained == 0 or self._scalers[hz].med is None:
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
                    warnings=[f"BME WARMING UP — modèle non entraîné ({n_tr}/{_MIN_TRAIN} samples)"],
                    features_snapshot=features_snapshot,
                )
                results.append(out)
                continue

            # Scaling + prédiction
            X = self._scalers[hz].transform(vec.reshape(1, -1))
            probs = self._models[hz].predict_proba(X)[0]   # (3,) — UP, DOWN, RANGE

            # Mode contrarian : inverser UP↔DOWN (val_set OU override prod)
            contrarian = self._contrarian.get(hz, False) or self._prod_contrarian.get(hz, False)
            if contrarian:
                probs[0], probs[1] = probs[1], probs[0]

            prob_up, prob_down, prob_range = float(probs[0]), float(probs[1]), float(probs[2])
            # Renormalisation
            tot = prob_up + prob_down + prob_range
            if tot > 1e-8:
                prob_up /= tot; prob_down /= tot; prob_range /= tot

            dominant = _dominant_from_prob3(prob_up, prob_down, prob_range)
            # Confidence gate : prédiction directionnelle seulement si conviction suffisante
            if dominant != "RANGE" and max(prob_up, prob_down) < 0.42:
                dominant = "RANGE"
            confidence = min(0.75, max(prob_up, prob_down, prob_range))

            # Top factors (approximation par magnitude des poids)
            feat_names = [
                "mom_4h", "mom_8h", "mom_24h", "mom_48h", "mom_accel",
                "vol_24h", "rsi_14", "bb_pct",
                "gex_near", "dex", "iv_rank", "pcr", "mopi", "flip_dist",
                "funding", "futures_oi", "spot_vol", "mp_dist",
                "mopi_div_type", "mopi_div_str",
                "mom×regime", "iv×vol",
            ]
            W_pred = self._models[hz].W[_LABEL_IDX.get(dominant, 0)]
            top_idx_feat = np.argsort(np.abs(W_pred * X[0]))[::-1][:4]
            top_factors = [feat_names[i] for i in top_idx_feat if i < len(feat_names)]

            warnings = [f"BME — {n_tr} samples | val_WR={self._val_wr.get(hz, 0):.1%}"]
            if contrarian:
                warnings.append(f"CONTRARIAN MODE actif — WR dir={self._val_wr.get(hz, 0):.1%}")

            # Infos debug
            explanation = {
                "btc_momentum_24h":  round(btc_f.get("mom_24h") or 0.0, 4),
                "btc_volatility_24h": round(btc_f.get("vol_24h") or 0.0, 5),
                "btc_rsi_14":        round(btc_f.get("rsi_14") or 50.0, 1),
                "btc_bb_pct":        round(btc_f.get("bb_pct") or 0.0, 3),
                "contrarian_mode":   contrarian,
                "val_winrate":       round(self._val_wr.get(hz, 0.0), 3),
                "n_train":           n_tr,
                "trained_at":        trained,
            }

            out = ArenaOutput(
                model_name=self.name,
                version=self.version,
                timestamp=datetime.now(timezone.utc).isoformat(),
                horizon=hz,
                spot_at_prediction=spot,
                prob_up=round(prob_up, 3),
                prob_down=round(prob_down, 3),
                prob_range=round(prob_range, 3),
                confidence=round(confidence, 3),
                dominant_scenario=dominant,
                data_coverage=min(1.0, n_tr / 500.0),
                top_factors=top_factors,
                warnings=warnings,
                features_snapshot=features_snapshot,
                explanation=explanation,
            )
            results.append(out)
        return results

    def get_status(self) -> dict:
        """Statut du moteur pour diagnostic."""
        return {
            "name": self.name,
            "version": self.version,
            "horizons": {
                hz: {
                    "trained_at":          self._trained_at.get(hz, 0),
                    "n_train":             self._n_train.get(hz, 0),
                    "val_wr":              round(self._val_wr.get(hz, 0.0), 3),
                    "contrarian":          self._contrarian.get(hz, False),
                    "prod_contrarian":     self._prod_contrarian.get(hz, False),
                    "effective_contrarian": (
                        self._contrarian.get(hz, False) or self._prod_contrarian.get(hz, False)
                    ),
                    # B1: contrarian_decided_at pour le frontend (badge CONTRARIAN)
                    "contrarian_decided_at": self._contrarian_decided_at.get(hz, 0),
                    "model_ready":         self._trained_at.get(hz, 0) > 0,
                    "age_hours":           round((time.time() - self._trained_at.get(hz, 0)) / 3600, 1),
                }
                for hz in _HORIZONS
            },
        }

    def backtest(self, horizon: str, last_n: int = 200) -> dict:
        """
        Backtest OUT-OF-SAMPLE avec embargo (Phase B1).

        Protocole :
        1. Reconstruit la frontiere train/OOS identique a train() : split 80%
           des donnees triees par timestamp.
        2. Embargo : exclut tout sample dont ts <= train_end_ts + horizon_secs
           (la fenetre d'outcome chevauche le train).
        3. En mode contrarian : evalue uniquement les samples posterieurs a
           contrarian_decided_at (la decision etait post-hoc sinon).
        4. Retourne has_edge (calcul Wilson serveur) + wilson_lb pour le frontend.

        Champs retournes :
          n_out_of_sample     : samples evalues (apres split + embargo)
          n_overlap_excluded  : samples exclus par embargo
          eval_is_oos         : True (garantie de non-contamination)
          dir_winrate         : WR directionnel (null si insuffisant)
          has_edge            : bool serveur (wilson_lower > 0.50 et n >= 30)
          wilson_lb           : borne inferieure Wilson (observabilite)
          contrarian_mode     : bool
          contrarian_decided_at : ts d'activation (0 si inactif)
        """
        data, spots = self._get_training_data(horizon)
        if not data or self._trained_at.get(horizon, 0) == 0:
            return {"error": "modele non entraine"}

        # ── Horizon en secondes pour embargo ─────────────────────────────────
        _horizon_secs = {"4h": 4*3600, "24h": 24*3600, "72h": 72*3600}
        embargo_secs = _horizon_secs.get(horizon, 24*3600)

        # ── Reconstruction frontiere train identique a train() ────────────────
        # data est trie DESC par timestamp depuis _get_training_data (LIMIT 800)
        # on inverse pour avoir ASC
        data_asc = list(reversed(data))
        n_total = len(data_asc)
        split_internal = int(n_total * 0.8)
        if split_internal >= n_total:
            return {"error": "pas assez de samples pour split OOS"}

        train_part = data_asc[:split_internal]
        oos_part   = data_asc[split_internal:]

        # ts de fin du train = dernier timestamp du train set
        train_end_ts = train_part[-1][0] if train_part else 0
        embargo_cutoff = train_end_ts + embargo_secs

        # ── Contrarian : filtrer uniquement les samples post-decision ─────────
        contrarian = self._contrarian.get(horizon, False) or self._prod_contrarian.get(horizon, False)
        contrarian_decided_at = self._contrarian_decided_at.get(horizon, 0)

        # ── Filtrage OOS + embargo ────────────────────────────────────────────
        n_overlap_excluded = 0
        oos_filtered = []
        for row in oos_part:
            ts = row[0]
            if ts <= embargo_cutoff:
                n_overlap_excluded += 1
                continue
            # En mode contrarian : ignorer les samples anterieurs a la decision
            if contrarian and contrarian_decided_at > 0 and ts <= contrarian_decided_at:
                n_overlap_excluded += 1
                continue
            oos_filtered.append(row)

        # ── Evaluation OOS ────────────────────────────────────────────────────
        if not oos_filtered:
            if contrarian and contrarian_decided_at > 0:
                return {
                    "n_out_of_sample": 0,
                    "n_overlap_excluded": n_overlap_excluded,
                    "eval_is_oos": True,
                    "dir_winrate": None,
                    "reason": "contrarian_insufficient_oos",
                    "has_edge": False,
                    "wilson_lb": None,
                    "contrarian_mode": contrarian,
                    "contrarian_decided_at": contrarian_decided_at,
                    "horizon": horizon,
                }
            return {"error": "aucun sample OOS valide apres embargo"}

        X_raw, y_str = [], []
        for ts, spot, opt_feats, label in oos_filtered:
            btc_f = extract_btc_features(spot, ts, spots)
            opt_feats_copy = dict(opt_feats)
            opt_feats_copy["spot"] = spot
            vec = build_feature_vector(btc_f, opt_feats_copy)
            if vec is not None and label in _LABEL_IDX:
                X_raw.append(vec)
                y_str.append(label)

        if not X_raw:
            return {"error": "aucun sample OOS avec features valides"}

        X = self._scalers[horizon].transform(np.array(X_raw, dtype=np.float64))
        probs = self._models[horizon].predict_proba(X)
        if contrarian:
            probs[:, [0, 1]] = probs[:, [1, 0]]

        preds = probs.argmax(axis=1)
        pred_str = [_IDX_LABEL[p] for p in preds]

        n_oos = len(y_str)
        dir_att = sum(1 for p, r in zip(pred_str, y_str) if p != "RANGE" and r != "RANGE")
        dir_cor = sum(1 for p, r in zip(pred_str, y_str) if p != "RANGE" and r != "RANGE" and p == r)
        dir_wr  = round(dir_cor / dir_att, 3) if dir_att > 0 else None

        # ── Wilson bounds + has_edge (calcul serveur — B1.4) ─────────────────
        wb_lb = round(wilson_lower(dir_wr, dir_att), 3) if dir_wr is not None and dir_att > 0 else None
        edge = _has_edge(dir_wr, dir_att)  # n >= 30 ET wilson_lower > 0.50

        from collections import Counter
        return {
            "n_out_of_sample":      n_oos,
            "n_overlap_excluded":   n_overlap_excluded,
            "eval_is_oos":          True,
            "n_dir_attempted":      dir_att,
            "dir_winrate":          dir_wr,
            "has_edge":             edge,
            "wilson_lb":            wb_lb,
            "pred_distribution":    dict(Counter(pred_str)),
            "true_distribution":    dict(Counter(y_str)),
            "contrarian_mode":      contrarian,
            "contrarian_decided_at": contrarian_decided_at,
            "horizon":              horizon,
        }


# ─── Module-level singleton ───────────────────────────────────────────────────

_bme_instance: Optional[BTCMomentumEngine] = None


def get_bme() -> BTCMomentumEngine:
    global _bme_instance
    if _bme_instance is None:
        _bme_instance = BTCMomentumEngine()
    return _bme_instance


def train_all() -> dict:
    """Entraîne tous les horizons. Appeler au démarrage ou depuis un endpoint."""
    bme = get_bme()
    results = {}
    for hz in _HORIZONS:
        ok = bme.train(hz)
        results[hz] = "trained" if ok else "skipped"
    return results
