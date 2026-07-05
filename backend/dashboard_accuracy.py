"""
Phase 6 — Mesure de la valeur réelle du dashboard.

4 métriques séparées (règle Mamos) :
  1. direction_score  — le marché est-il allé dans la direction annoncée ?
  2. objectif_score   — la cible affichée a-t-elle été atteinte ?
  3. emq_score        — Expected Move Quality (amplitude estimée ≈ amplitude réelle ?)
  4. theoretical_return — rendement moyen si l'utilisateur avait suivi le biais

Les 3 premières métriques ne doivent JAMAIS être mélangées en un seul score.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

_DATA_DIR = Path("/data")
EXECUTIVE_LOG_PATH = (
    (_DATA_DIR / "executive_log.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "executive_log.jsonl")
)
EXECUTIVE_PENDING_PATH = (
    (_DATA_DIR / "executive_pending.json") if _DATA_DIR.exists()
    else (Path(__file__).parent / "executive_pending.json")
)

log = logging.getLogger(__name__)

BIAS_TYPES = ["BULL FORT", "Bull modéré", "Neutre", "Bear modéré", "BEAR FORT"]

# Mapping régime texte interne → format standard Regime Segmentation
_GEX_REGIME_MAP = {
    "AMPLIFICATEUR": "Negative_Gamma",
    "STABILISANT":   "Positive_Gamma",
    "NEUTRE":        "Neutral",
}

BIAS_REGIME_MATRIX_REGIMES = [
    "Positive_Gamma", "Negative_Gamma", "Neutral",
    "Vol_Expansion", "Vol_Contraction", "Panic",
]

_CONTEXT_FIELDS = [
    "gex_regime", "gex_near", "iv_rank", "vol_regime",
    "panic", "dex_score", "mopi_score",
    "flip_distance_pct", "max_pain_distance_pct",
]


def _classify_scores(
    bias_color: str,
    btc_at_log: float,
    target: float,
    invalidation: float,
    btc_24h: float,
) -> dict:
    """
    Retourne les 4 métriques de qualité de prédiction — JAMAIS un verdict unique.

    direction_score  : confirmed | invalidated | partial
    objectif_score   : confirmed | invalidated | partial
    emq_score        : good | bad | partial
    theoretical_return: float (rendement % si suivi du biais)
    """
    if btc_at_log <= 0 or btc_24h <= 0:
        return {
            "direction_score": "partial",
            "objectif_score": "partial",
            "emq_score": "partial",
            "theoretical_return": 0.0,
            "expected_amplitude_pct": 0.0,
            "actual_amplitude_pct": 0.0,
            "verdict": "partial",  # rétrocompatibilité log
        }

    chg_pct = (btc_24h - btc_at_log) / btc_at_log * 100

    # ── 1. Direction Score ────────────────────────────────────────────────────
    # Question : le marché est-il allé dans la bonne direction ?
    if bias_color == "yellow":
        # Neutre = confirmé si le marché est resté stable
        if abs(chg_pct) < 1.0:
            direction_score = "confirmed"
        elif abs(chg_pct) >= 2.0:
            direction_score = "invalidated"
        else:
            direction_score = "partial"
    elif bias_color == "green":
        if chg_pct > 0.1:
            direction_score = "confirmed"
        elif chg_pct < -0.5:
            direction_score = "invalidated"
        else:
            direction_score = "partial"
    else:  # red (bear)
        if chg_pct < -0.1:
            direction_score = "confirmed"
        elif chg_pct > 0.5:
            direction_score = "invalidated"
        else:
            direction_score = "partial"

    # ── 2. Objectif Score ─────────────────────────────────────────────────────
    # Question : la cible exacte affichée a-t-elle été atteinte ?
    # NB : un objectif non-atteint sur BULL FORT est attendu ; sur Bull modéré c'est plus problématique.
    #      C'est au rapport de l'interpréter par biais — ici on enregistre le fait brut.
    if bias_color == "green":
        if btc_24h >= target:
            objectif_score = "confirmed"
        elif btc_24h <= invalidation:
            objectif_score = "invalidated"
        else:
            objectif_score = "partial"
    elif bias_color == "red":
        if btc_24h <= target:
            objectif_score = "confirmed"
        elif btc_24h >= invalidation:
            objectif_score = "invalidated"
        else:
            objectif_score = "partial"
    else:  # yellow
        dist_24h = abs(btc_24h - target)
        dist_entry = max(abs(target - btc_at_log), btc_at_log * 0.001)
        if dist_24h / btc_at_log * 100 <= 1.0:
            objectif_score = "confirmed"
        elif dist_24h > dist_entry * 1.5:
            objectif_score = "invalidated"
        else:
            objectif_score = "partial"

    # ── 3. Expected Move Quality (EMQ) ────────────────────────────────────────
    # Question : l'amplitude estimée était-elle correcte ?
    # Prédire +0.2% et obtenir +8% = mauvaise prédiction même si direction correcte.
    expected_amplitude_pct = abs(target - btc_at_log) / btc_at_log * 100
    actual_amplitude_pct = abs(chg_pct)

    if expected_amplitude_pct > 0.05:
        ratio = actual_amplitude_pct / expected_amplitude_pct
        # Fourchette acceptable : entre 35% et 250% de l'amplitude attendue
        if 0.35 <= ratio <= 2.5:
            emq_score = "good"
        elif ratio < 0.1 or ratio > 5.0:
            emq_score = "bad"
        else:
            emq_score = "partial"
    else:
        emq_score = "partial"

    # ── 4. Rendement théorique ────────────────────────────────────────────────
    # Question : si un utilisateur avait suivi le biais du dashboard, quel résultat ?
    if bias_color == "green":
        theoretical_return = chg_pct        # position longue
    elif bias_color == "red":
        theoretical_return = -chg_pct       # position courte (short)
    else:
        theoretical_return = 0.0            # neutre = pas de position

    return {
        "direction_score": direction_score,
        "objectif_score": objectif_score,
        "emq_score": emq_score,
        "theoretical_return": round(theoretical_return, 2),
        "expected_amplitude_pct": round(expected_amplitude_pct, 2),
        "actual_amplitude_pct": round(actual_amplitude_pct, 2),
        "verdict": direction_score,          # rétrocompatibilité
    }


def _regime_tags_from_context(entry: dict) -> List[str]:
    """Dérive les tags de régime depuis les champs de contexte sauvegardés."""
    tags = []
    gex_r = entry.get("gex_regime")
    if gex_r in ("Positive_Gamma", "Negative_Gamma", "Neutral"):
        tags.append(gex_r)
    if entry.get("panic"):
        tags.append("Panic")
    vol_r = entry.get("vol_regime")
    if vol_r in ("Vol_Expansion", "Vol_Contraction"):
        tags.append(vol_r)
    return tags


def _append_log(entry: dict) -> None:
    try:
        with open(EXECUTIVE_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning(f"[accuracy] write error: {e}")


class DashboardAccuracyTracker:
    def __init__(self):
        self._pending: List[dict] = []
        self._load_pending()

    # ── Persistance ──────────────────────────────────────────────────────────

    def _load_pending(self):
        if not EXECUTIVE_PENDING_PATH.exists():
            return
        try:
            with open(EXECUTIVE_PENDING_PATH) as f:
                self._pending = json.load(f)
            log.info(f"[accuracy] {len(self._pending)} entrées pendantes chargées")
        except Exception as e:
            log.warning(f"[accuracy] chargement pending: {e}")

    def _save_pending(self):
        try:
            with open(EXECUTIVE_PENDING_PATH, "w") as f:
                json.dump(self._pending, f, default=str)
        except Exception as e:
            log.warning(f"[accuracy] sauvegarde pending: {e}")

    # ── Enregistrement horaire ────────────────────────────────────────────────

    def log_summary(
        self,
        bias: str,
        bias_color: str,
        alignment: int,
        target: float,
        invalidation: float,
        btc_price: float,
        # Contexte régime — Phase 1 apprentissage conditionnel
        gex_regime: Optional[str] = None,
        gex_near: Optional[float] = None,
        iv_rank: Optional[float] = None,
        vol_regime: Optional[str] = None,
        panic: Optional[bool] = None,
        dex_score: Optional[float] = None,
        mopi_score: Optional[float] = None,
        flip_distance_pct: Optional[float] = None,
        max_pain_distance_pct: Optional[float] = None,
        # Métadonnées Arena — Phase 5 visibilité apprentissage
        arena_leader: Optional[str] = None,
        arena_rank: Optional[int] = None,
        adaptive_weights_mode: Optional[str] = None,
        adaptive_weights_active: Optional[bool] = None,
    ) -> None:
        """Enregistre le résumé exécutif courant. Appelé toutes les heures."""
        entry = {
            "ts":           datetime.now(timezone.utc).isoformat(),
            "btc":          round(btc_price, 0),
            "bias":         bias,
            "bias_color":   bias_color,
            "alignment":    alignment,
            "target":       round(target, 0) if target is not None else None,
            "invalidation": round(invalidation, 0) if invalidation is not None else None,
            "validated":    False,
            # Contexte régime enrichi
            "gex_regime":            gex_regime,
            "gex_near":              round(gex_near, 0) if gex_near is not None else None,
            "iv_rank":               round(iv_rank, 1) if iv_rank is not None else None,
            "vol_regime":            vol_regime,
            "panic":                 panic,
            "dex_score":             round(dex_score, 1) if dex_score is not None else None,
            "mopi_score":            round(mopi_score, 1) if mopi_score is not None else None,
            "flip_distance_pct":     round(flip_distance_pct, 2) if flip_distance_pct is not None else None,
            "max_pain_distance_pct": round(max_pain_distance_pct, 2) if max_pain_distance_pct is not None else None,
            # Métadonnées Arena
            "arena_leader":           arena_leader,
            "arena_rank":             arena_rank,
            "adaptive_weights_mode":  adaptive_weights_mode,
            "adaptive_weights_active": adaptive_weights_active,
        }
        self._pending.append(entry)
        self._save_pending()
        t_str = f"${target:,.0f}" if target is not None else "N/A"
        i_str = f"${invalidation:,.0f}" if invalidation is not None else "N/A"
        log.info(f"[accuracy] log biais={bias} BTC=${btc_price:,.0f} cible={t_str} invalidation={i_str}")

    # ── Loop de validation ────────────────────────────────────────────────────

    async def run_validation_loop(self):
        """Vérifie toutes les 5 min les entrées pendantes à +24h."""
        while True:
            await asyncio.sleep(300)
            await self._check_pending()

    async def _fetch_btc_price(self) -> float:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            resp.raise_for_status()
            return float(resp.json()["price"])

    async def _check_pending(self):
        if not self._pending:
            return

        try:
            btc_now = await self._fetch_btc_price()
        except Exception as e:
            log.warning(f"[accuracy] fetch BTC: {e}")
            return

        now           = datetime.now(timezone.utc)
        changed       = False
        still_pending = []

        for entry in self._pending:
            if entry.get("validated"):
                continue
            try:
                ts = datetime.fromisoformat(entry["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                still_pending.append(entry)
                continue

            if (now - ts).total_seconds() < 86400:
                still_pending.append(entry)
                continue

            # +24h atteint — calculer les 4 métriques
            btc_at  = entry["btc"]
            chg_pct = (btc_now - btc_at) / btc_at * 100 if btc_at else 0
            scores  = _classify_scores(
                entry["bias_color"],
                btc_at,
                entry["target"],
                entry["invalidation"],
                btc_now,
            )

            completed = {
                **entry,
                "btc_24h":                  round(btc_now, 0),
                "chg_24h_pct":              round(chg_pct, 2),
                # 4 métriques séparées
                "direction_score":          scores["direction_score"],
                "objectif_score":           scores["objectif_score"],
                "emq_score":                scores["emq_score"],
                "theoretical_return":       scores["theoretical_return"],
                "expected_amplitude_pct":   scores["expected_amplitude_pct"],
                "actual_amplitude_pct":     scores["actual_amplitude_pct"],
                # rétrocompatibilité
                "verdict":                  scores["verdict"],
                "direction_correct":        scores["direction_score"] == "confirmed",
                "target_reached":           scores["objectif_score"] == "confirmed",
                "invalidation_hit":         scores["objectif_score"] == "invalidated",
                "validated":                True,
                "validated_at":             now.isoformat(),
            }
            _append_log(completed)
            changed = True
            log.info(
                f"[accuracy] dir={scores['direction_score']} obj={scores['objectif_score']} "
                f"emq={scores['emq_score']} ret={scores['theoretical_return']:+.2f}% "
                f"biais={entry['bias']} BTC${btc_at:,.0f}→${btc_now:,.0f} ({chg_pct:+.2f}%)"
            )

        if changed:
            self._pending = still_pending
            self._save_pending()

    # ── Comptages Phase 0 ─────────────────────────────────────────────────────

    def get_pending_count(self) -> int:
        return len(self._pending)

    def get_finalized_count(self) -> int:
        if not EXECUTIVE_LOG_PATH.exists():
            return 0
        try:
            with open(EXECUTIVE_LOG_PATH) as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    # ── Statistiques ─────────────────────────────────────────────────────────

    def get_accuracy_stats(self, days: int = 7) -> Dict[str, dict]:
        """Retourne les 4 métriques par type de biais pour les N derniers jours."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stats: Dict[str, Dict] = {}

        if not EXECUTIVE_LOG_PATH.exists():
            return {}

        try:
            with open(EXECUTIVE_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not entry.get("validated"):
                        continue
                    try:
                        ts = datetime.fromisoformat(entry["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue

                    bias = entry.get("bias", "Inconnu")
                    if bias not in stats:
                        stats[bias] = {
                            "total": 0,
                            "dir_confirmed": 0, "dir_invalidated": 0, "dir_partial": 0,
                            "obj_confirmed": 0, "obj_invalidated": 0, "obj_partial": 0,
                            "emq_good": 0, "emq_bad": 0, "emq_partial": 0,
                            "theoretical_return_sum": 0.0,
                            # rétrocompatibilité anciens logs
                            "confirmed": 0, "partial": 0, "invalidated": 0,
                        }
                    s = stats[bias]
                    s["total"] += 1

                    # Nouveau format (direction_score présent)
                    if "direction_score" in entry:
                        d = entry["direction_score"]
                        s[f"dir_{d}"] = s.get(f"dir_{d}", 0) + 1

                        o = entry.get("objectif_score", "partial")
                        s[f"obj_{o}"] = s.get(f"obj_{o}", 0) + 1

                        e = entry.get("emq_score", "partial")
                        key = f"emq_{e}"
                        s[key] = s.get(key, 0) + 1

                        s["theoretical_return_sum"] += entry.get("theoretical_return", 0.0)
                        # rétrocompatibilité
                        s[d] = s.get(d, 0) + 1
                    else:
                        # Ancien format — verdict unique → direction uniquement
                        v = entry.get("verdict", "partial")
                        dir_map = {"confirmed": "dir_confirmed", "invalidated": "dir_invalidated", "partial": "dir_partial"}
                        s[dir_map.get(v, "dir_partial")] = s.get(dir_map.get(v, "dir_partial"), 0) + 1
                        s[v] = s.get(v, 0) + 1
                        # Pas d'EMQ ni rendement sur anciens logs
                        if entry.get("direction_correct"):
                            s["theoretical_return_sum"] += entry.get("chg_24h_pct", 0.0)

        except Exception as e:
            log.warning(f"[accuracy] read stats: {e}")

        return stats

    # ── Phase 3 — Matrice Biais × Régime ─────────────────────────────────────

    def compute_bias_regime_matrix(self, days: int = 90) -> Dict:
        """Matrice biais × régime — EV/WR/PF par combinaison.

        N < 30 → insufficient (données en accumulation).
        Garde-fou Phase 5 : si enriched < 30 ou couverture < 80%, retourne INSUFFICIENT_DATA.
        """
        from collections import defaultdict

        # ── Garde-fou Phase 5 ───────────────────────────────────────────────
        health = self.get_data_health()
        n_enriched_total = health["enriched_count"]
        enrichment_pct = health["enrichment_rate_pct"]
        if n_enriched_total < 30 or enrichment_pct < 80:
            return {
                "status": "INSUFFICIENT_DATA",
                "matrix": {},
                "meta": {
                    "n_total": health["total_snapshots"],
                    "n_enriched": n_enriched_total,
                    "enrichment_rate_pct": enrichment_pct,
                    "days": days,
                    "message": (
                        f"Données insuffisantes — {n_enriched_total} snapshots enrichis "
                        f"({enrichment_pct:.1f}% de couverture). "
                        "Seuil requis : 30 snapshots enrichis ET ≥80% de couverture. "
                        "La matrice ne donnera aucune interprétation avant ce seuil."
                    ),
                },
            }
        # ────────────────────────────────────────────────────────────────────

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        buckets: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        n_total = 0
        n_enriched = 0

        if not EXECUTIVE_LOG_PATH.exists():
            return {"status": "NO_DATA", "matrix": {}, "meta": {"n_total": 0, "n_enriched": 0}}

        try:
            with open(EXECUTIVE_LOG_PATH) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if not entry.get("validated"):
                        continue
                    try:
                        ts = datetime.fromisoformat(entry["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue

                    n_total += 1
                    tags = _regime_tags_from_context(entry)
                    if not tags:
                        continue

                    n_enriched += 1
                    bias = entry.get("bias", "Inconnu")
                    ret = entry.get("theoretical_return", 0.0) or 0.0
                    for regime in tags:
                        buckets[bias][regime].append(ret)
        except Exception as e:
            log.warning(f"[accuracy] compute_bias_regime_matrix: {e}")

        matrix: Dict[str, Dict] = {}
        for bias in BIAS_TYPES:
            if bias not in buckets:
                continue
            matrix[bias] = {}
            for regime in BIAS_REGIME_MATRIX_REGIMES:
                returns = buckets[bias].get(regime, [])
                if not returns:
                    continue
                n = len(returns)
                if n < 5:
                    matrix[bias][regime] = {"n": n, "insufficient": True}
                    continue
                wins = [r for r in returns if r > 0]
                losses = [r for r in returns if r <= 0]
                winrate = round(len(wins) / n * 100, 1)
                ev = round(sum(returns) / n, 2)
                total_gain = sum(wins)
                total_loss = abs(sum(losses))
                pf = round(total_gain / total_loss, 2) if total_loss > 0 else None
                matrix[bias][regime] = {
                    "n": n,
                    "winrate": winrate,
                    "ev": ev,
                    "profit_factor": pf,
                    "insufficient": n < 30,
                }

        return {
            "status": "OK" if n_total > 0 else "NO_DATA",
            "matrix": matrix,
            "meta": {
                "n_total": n_total,
                "n_enriched": n_enriched,
                "days": days,
                "coverage_pct": round(n_enriched / n_total * 100, 1) if n_total > 0 else 0.0,
                "note": "N<30 = insufficient — accumulation en cours",
            },
        }

    # ── Visibilité progression apprentissage cellule par cellule ─────────────

    def compute_bias_regime_matrix_progress(self) -> dict:
        """Progression N outcomes finalisés / 30 pour chaque cellule bias×régime.

        RÈGLE : status = f(N_outcomes_finalisés) JAMAIS f(N_observations).
        Seuls les outcomes validés (+24h) depuis executive_log.jsonl comptent.
        Les pending (non-finalisés) sont comptés séparément comme n_obs (informatif).
        Statuts : EMPTY(0) / COLLECTING(1-29) / EXPLOITABLE(30-99) / ROBUST(100+).
        """
        TARGET_N = 30
        # Outcomes finalisés (validated=True) — source de vérité pour le statut
        counts: Dict[str, Dict[str, int]] = {}
        # Observations en attente de validation (informatif seulement)
        obs_counts: Dict[str, Dict[str, int]] = {}
        for bias in BIAS_TYPES:
            counts[bias] = {r: 0 for r in BIAS_REGIME_MATRIX_REGIMES}
            obs_counts[bias] = {r: 0 for r in BIAS_REGIME_MATRIX_REGIMES}

        first_ts: Optional[datetime] = None
        last_ts:  Optional[datetime] = None
        total_finalized = 0

        def _count_entry(entry: dict, target_counts: Dict[str, Dict[str, int]]) -> None:
            nonlocal first_ts, last_ts, total_finalized
            tags = _regime_tags_from_context(entry)
            if not tags:
                return
            bias = entry.get("bias")
            if not bias or bias not in BIAS_TYPES:
                return
            if target_counts is counts:
                # Suivi du temps uniquement sur les finalisés (source de vérité)
                total_finalized += 1
                try:
                    ts = datetime.fromisoformat(entry.get("ts", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                except Exception:
                    pass
            for regime in tags:
                if regime in BIAS_REGIME_MATRIX_REGIMES:
                    target_counts[bias][regime] += 1

        # Observations pending — comptées séparément, jamais dans le statut
        for entry in self._pending:
            _count_entry(entry, obs_counts)

        # Outcomes finalisés — seuls ceux-ci déterminent le statut
        if EXECUTIVE_LOG_PATH.exists():
            try:
                with open(EXECUTIVE_LOG_PATH) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            _count_entry(json.loads(line), counts)
                        except Exception:
                            continue
            except Exception as e:
                log.warning(f"[accuracy] compute_progress: {e}")

        if first_ts and last_ts and total_finalized > 0:
            days_elapsed = max(1.0, (last_ts - first_ts).total_seconds() / 86400)
            global_rate = total_finalized / days_elapsed
        else:
            days_elapsed = 0.0
            global_rate = 1.0  # fallback conservateur : ~1 outcome/jour

        cells = []
        for bias in BIAS_TYPES:
            for regime in BIAS_REGIME_MATRIX_REGIMES:
                n = counts[bias][regime]          # outcomes finalisés — seul qui compte
                n_obs = obs_counts[bias][regime]   # observations pending (informatif)
                progress_pct = min(100, round(n / TARGET_N * 100, 1))

                if n == 0:
                    status = "EMPTY"
                elif n < TARGET_N:
                    status = "COLLECTING"
                elif n < 100:
                    status = "EXPLOITABLE"
                else:
                    status = "ROBUST"

                if n >= TARGET_N:
                    eta_days = None
                elif global_rate > 0 and total_finalized > 0 and n > 0:
                    cell_rate = global_rate * (n / total_finalized)
                    cell_rate = max(cell_rate, 0.01)
                    eta_days = round((TARGET_N - n) / cell_rate)
                else:
                    eta_days = None

                cells.append({
                    "bias":         bias,
                    "regime":       regime,
                    "n":            n,        # outcomes validés (source de vérité statut)
                    "n_obs":        n_obs,    # observations pending (informatif)
                    "target_n":     TARGET_N,
                    "progress_pct": progress_pct,
                    "status":       status,
                    "eta_days":     eta_days,
                })

        cells_sorted = sorted(cells, key=lambda c: c["n"], reverse=True)

        status_counts: Dict[str, int] = {"EMPTY": 0, "COLLECTING": 0, "EXPLOITABLE": 0, "ROBUST": 0}
        for c in cells:
            status_counts[c["status"]] += 1

        top_10 = [c for c in cells_sorted if c["n"] > 0][:10]
        bottom_10 = sorted(cells, key=lambda c: c["n"])[:10]
        almost_exploitable = [c for c in cells_sorted if TARGET_N - 10 <= c["n"] < TARGET_N]
        dead_zones = [c for c in cells if c["n"] < 5]

        n_exploitable = status_counts.get("EXPLOITABLE", 0) + status_counts.get("ROBUST", 0)
        total_cells = len(cells)
        progression_pct = round(n_exploitable / total_cells * 100, 1) if total_cells > 0 else 0.0
        collection_status = "COLLECTE" if n_exploitable < total_cells else "EXPLOITABLE"

        total_obs_pending = sum(
            obs_counts[b][r]
            for b in BIAS_TYPES
            for r in BIAS_REGIME_MATRIX_REGIMES
        )

        return {
            "cells": cells_sorted,
            "n_exploitable": n_exploitable,
            "total_cells": total_cells,
            "progression_pct": progression_pct,
            "collection_status": collection_status,
            "summary": {
                "total_cells":           total_cells,
                "total_finalized":       total_finalized,   # outcomes validés avec régime
                "total_obs_pending":     total_obs_pending, # observations non-encore validées
                "days_elapsed":          round(days_elapsed, 1),
                "global_rate_per_day":   round(global_rate, 2),
                **status_counts,
            },
            "top_10_advanced":       top_10,
            "bottom_10_empty":       bottom_10,
            "almost_exploitable":    almost_exploitable,
            "dead_zones":            dead_zones,
            "meta": {
                "target_n":  TARGET_N,
                "biases":    BIAS_TYPES,
                "regimes":   BIAS_REGIME_MATRIX_REGIMES,
                "note":      "n = outcomes validés (+24h). n_obs = observations pending (informatif). Statut basé sur n uniquement.",
                "data_note": "Les outcomes finalisés avant l'ajout du champ gex_regime ont n_regime=0. L'accumulation augmente naturellement avec les nouvelles finalisations.",
            },
        }

    # ── Phase 1/5 — Santé des données régime ─────────────────────────────────

    def _load_all_entries(self) -> List[dict]:
        """Charge pending + finalisés dans l'ordre chronologique."""
        entries: List[dict] = list(self._pending)
        if EXECUTIVE_LOG_PATH.exists():
            try:
                with open(EXECUTIVE_LOG_PATH) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            continue
            except Exception as e:
                log.warning(f"[accuracy] _load_all_entries: {e}")
        return entries

    # Champs dont l'absence est une condition de marché, pas un bug pipeline
    _NOT_APPLICABLE_FIELDS: frozenset = frozenset({"flip_distance_pct"})

    def get_data_health(self) -> dict:
        """Couverture double (globale + depuis activation) — /api/data_health V2.

        Phase 1 : global vs depuis activation
        Phase 2 : MISSING_DATA vs NOT_APPLICABLE
        Phase 3 : section enriched_pipeline (count, coverage, last_snapshot_min_ago)
        Phase 5 : pipeline_stalled si aucun enrichi depuis > 3h
        """
        all_entries = self._load_all_entries()
        total_global = len(all_entries)

        # Trouver le timestamp d'activation (premier snapshot enrichi)
        activation_ts: Optional[str] = None
        for entry in sorted(all_entries, key=lambda e: e.get("ts", "")):
            if any(entry.get(f) is not None for f in _CONTEXT_FIELDS):
                activation_ts = entry.get("ts", "")
                break

        # Scinder pre/post activation
        if activation_ts:
            post_activation = [e for e in all_entries if e.get("ts", "") >= activation_ts]
        else:
            post_activation = []
        total_since = len(post_activation)

        # Comptages globaux
        global_field_counts = {f: 0 for f in _CONTEXT_FIELDS}
        enriched_count_global = 0
        last_enriched_ts: Optional[str] = None

        for entry in all_entries:
            has_any = any(entry.get(f) is not None for f in _CONTEXT_FIELDS)
            if has_any:
                enriched_count_global += 1
                ts = entry.get("ts", "")
                if ts and (last_enriched_ts is None or ts > last_enriched_ts):
                    last_enriched_ts = ts
            for f in _CONTEXT_FIELDS:
                if entry.get(f) is not None:
                    global_field_counts[f] += 1

        # Comptages depuis activation
        since_field_counts = {f: 0 for f in _CONTEXT_FIELDS}
        enriched_count_since = 0
        for entry in post_activation:
            has_any = any(entry.get(f) is not None for f in _CONTEXT_FIELDS)
            if has_any:
                enriched_count_since += 1
            for f in _CONTEXT_FIELDS:
                if entry.get(f) is not None:
                    since_field_counts[f] += 1

        # Couverture par champ : cause OK / MISSING_DATA / NOT_APPLICABLE
        coverage = {}
        alerts = []
        for f in _CONTEXT_FIELDS:
            g_pct = round(global_field_counts[f] / total_global * 100, 1) if total_global > 0 else 0.0
            s_pct = round(since_field_counts[f] / total_since * 100, 1) if total_since > 0 else 0.0

            if f in self._NOT_APPLICABLE_FIELDS:
                cause = "NOT_APPLICABLE"
                status = "OK"
            elif s_pct >= 80:
                cause = "OK"
                status = "OK"
            elif total_since == 0 or s_pct == 0:
                cause = "MISSING_DATA"
                status = "ALERTE"
                alerts.append(f)
            else:
                cause = "MISSING_DATA"
                status = "PARTIEL"

            coverage[f] = {
                # backward compat
                "count": global_field_counts[f],
                "coverage_pct": g_pct,
                # V2
                "coverage_global_pct": g_pct,
                "count_since_activation": since_field_counts[f],
                "coverage_since_activation_pct": s_pct,
                "status": status,
                "cause": cause,
            }

        # Détection pipeline mort (Phase 5)
        pipeline_stalled = False
        stall_hours: Optional[float] = None
        if last_enriched_ts:
            try:
                last_dt = datetime.fromisoformat(last_enriched_ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                gap_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()
                stall_hours = round(gap_sec / 3600, 1)
                pipeline_stalled = gap_sec > 3 * 3600
            except Exception:
                pass

        # Statut global
        if pipeline_stalled:
            global_status = "ALERTE"
        elif alerts:
            global_status = "ALERTE"
        elif enriched_count_since >= 30:
            global_status = "OK"
        else:
            global_status = "COLLECTING"

        recent_enriched = global_field_counts.get("gex_regime", 0)
        days_to_significance = round(max(0, 900 - recent_enriched) / 24.0)
        last_snapshot_min_ago = round(stall_hours * 60) if stall_hours is not None else None

        return {
            "global_status": global_status,
            "total_snapshots": total_global,
            "enriched_count": recent_enriched,
            "enrichment_rate_pct": round(recent_enriched / total_global * 100, 1) if total_global > 0 else 0.0,
            "field_coverage": coverage,
            "alerts": alerts,
            "activation_ts": activation_ts,
            "first_enriched_ts": activation_ts,
            "last_enriched_ts": last_enriched_ts,
            "pipeline_stalled": pipeline_stalled,
            "stall_hours": stall_hours,
            "enriched_pipeline": {
                "count": enriched_count_since,
                "total_since_activation": total_since,
                "coverage_since_activation_pct": (
                    round(enriched_count_since / total_since * 100, 1) if total_since > 0 else 0.0
                ),
                "last_snapshot_min_ago": last_snapshot_min_ago,
            },
            "days_to_significance_estimate": days_to_significance,
            "flip_distance_pct_root_cause": (
                "flip_level=None : aucun zero-crossing GEX détecté dans la fenêtre ±10% du spot. "
                "Le marché est en régime all_gamma_negative (toutes les strikes ont un GEX négatif). "
                "flip_distance_pct reste None jusqu'à ce qu'un crossing apparaisse. "
                "C'est une condition de marché réelle, pas un bug de sérialisation."
            ),
            "note": (
                f"Objectif : 900 snapshots enrichis (~37 jours). "
                f"Actuellement : {recent_enriched}. "
                f"Reste : {days_to_significance} jours."
            ),
        }

    def get_enrichment_growth(self) -> dict:
        """Phase 4 — Croissance réelle du pipeline enrichi sur 24h — /api/enrichment_growth."""
        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)

        current = 0
        count_before_24h = 0

        for entry in self._load_all_entries():
            has_any = any(entry.get(f) is not None for f in _CONTEXT_FIELDS)
            if not has_any:
                continue
            current += 1
            ts_raw = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts <= cutoff_24h:
                    count_before_24h += 1
            except Exception:
                pass

        growth = current - count_before_24h
        return {
            "current": current,
            "24h_ago": count_before_24h,
            "growth": growth,
            "growing": growth > 0,
        }

    def get_data_health_history(self) -> dict:
        """Historique horaire des snapshots enrichis — /api/data_health_history.

        Retourne : snapshots enrichis par heure, couverture moyenne par heure,
        progression cumulative des champs.
        Permet de vérifier que le compteur monte réellement.
        """
        from collections import defaultdict

        hourly: Dict[str, dict] = defaultdict(lambda: {
            "total": 0, "enriched": 0,
            "field_counts": {f: 0 for f in _CONTEXT_FIELDS},
        })

        for entry in self._load_all_entries():
            ts_raw = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                hour_key = ts.strftime("%Y-%m-%dT%H:00Z")
            except Exception:
                hour_key = "unknown"

            bucket = hourly[hour_key]
            bucket["total"] += 1
            has_any = any(entry.get(f) is not None for f in _CONTEXT_FIELDS)
            if has_any:
                bucket["enriched"] += 1
            for f in _CONTEXT_FIELDS:
                if entry.get(f) is not None:
                    bucket["field_counts"][f] += 1

        hours_sorted = sorted(k for k in hourly if k != "unknown")
        cumulative_enriched = 0
        timeline = []
        for h in hours_sorted:
            b = hourly[h]
            cumulative_enriched += b["enriched"]
            field_coverage = {
                f: round(b["field_counts"][f] / b["total"] * 100, 1) if b["total"] > 0 else 0.0
                for f in _CONTEXT_FIELDS
            }
            timeline.append({
                "hour": h,
                "snapshots_total": b["total"],
                "snapshots_enriched": b["enriched"],
                "cumulative_enriched": cumulative_enriched,
                "field_coverage": field_coverage,
            })

        # Vérification que ça monte : compare dernier vs avant-dernier
        growing = None
        if len(timeline) >= 2:
            last = timeline[-1]["cumulative_enriched"]
            prev = timeline[-2]["cumulative_enriched"]
            growing = last > prev

        return {
            "timeline": timeline,
            "total_hours": len(timeline),
            "total_enriched_snapshots": cumulative_enriched,
            "growing": growing,
            "note": (
                "growing=True → le compteur monte correctement. "
                "growing=False → problème d'accumulation à investiguer."
            ),
        }

    def generate_accuracy_report(self, days: int = 7) -> str:
        stats = self.get_accuracy_stats(days)
        total_entries = sum(s["total"] for s in stats.values())

        if not stats or total_entries == 0:
            return (
                f"📊 **DASHBOARD ACCURACY ({days}j)**\n\n"
                "Aucune donnée validée — les validations arrivent après 24h par entrée.\n"
                "Les logs s'accumulent automatiquement toutes les heures."
            )

        # Agrégats globaux
        total_dir_ok  = sum(s.get("dir_confirmed", 0) for s in stats.values())
        total_obj_ok  = sum(s.get("obj_confirmed", 0) for s in stats.values())
        total_emq_ok  = sum(s.get("emq_good", 0) for s in stats.values())
        total_ret     = sum(s.get("theoretical_return_sum", 0.0) for s in stats.values())
        dir_rate      = total_dir_ok / total_entries * 100 if total_entries else 0
        obj_rate      = total_obj_ok / total_entries * 100 if total_entries else 0
        emq_rate      = total_emq_ok / total_entries * 100 if total_entries else 0
        avg_ret       = total_ret / total_entries if total_entries else 0.0

        def bar(rate: float) -> str:
            filled = int(rate // 20)
            return "🟩" * filled + "⬜" * (5 - filled)

        lines = [
            f"📊 **DASHBOARD ACCURACY ({days}j)** — {total_entries} snapshots validés",
            "",
            f"**▸ DIRECTION** {bar(dir_rate)} {dir_rate:.0f}% ({total_dir_ok}/{total_entries})",
            "  Le marché a suivi la direction annoncée",
            "",
            f"**▸ OBJECTIF** {bar(obj_rate)} {obj_rate:.0f}% ({total_obj_ok}/{total_entries})",
            "  Les cibles exactes ont été atteintes",
            "",
            f"**▸ EMQ (Amplitude)** {bar(emq_rate)} {emq_rate:.0f}% ({total_emq_ok}/{total_entries})",
            "  L'amplitude estimée correspondait au mouvement réel",
            "",
            f"**▸ RENDEMENT THÉORIQUE** {avg_ret:+.2f}% / snapshot",
            "  Résultat moyen si suivi systématique du biais",
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "**Par biais :**",
        ]

        for bias in BIAS_TYPES:
            if bias not in stats:
                continue
            s     = stats[bias]
            total = s["total"]
            if total == 0:
                continue

            d_rate = s.get("dir_confirmed",  0) / total * 100
            o_rate = s.get("obj_confirmed",  0) / total * 100
            e_rate = s.get("emq_good",        0) / total * 100
            r_avg  = s.get("theoretical_return_sum", 0.0) / total
            ret_str = f"{r_avg:+.1f}%"

            lines.append(
                f"  **{bias}** ({total}) "
                f"dir {d_rate:.0f}% | obj {o_rate:.0f}% | EMQ {e_rate:.0f}% | Δ {ret_str}"
            )

        # Diagnostics
        dir_issues = [
            b for b, s in stats.items()
            if s["total"] >= 3 and s.get("dir_confirmed", 0) / s["total"] < 0.50
        ]
        obj_issues = [
            b for b, s in stats.items()
            if s["total"] >= 3 and s.get("obj_confirmed", 0) / s["total"] < 0.30
        ]
        emq_issues = [
            b for b, s in stats.items()
            if s["total"] >= 3 and s.get("emq_good", 0) / s["total"] < 0.35
        ]

        if dir_issues:
            lines.append("\n🔴 **Direction < 50% — biais à recalibrer :**")
            for b in dir_issues:
                s = stats[b]
                r = s.get("dir_confirmed", 0) / s["total"] * 100
                lines.append(f"  • {b} : {r:.0f}%")

        if obj_issues:
            lines.append("\n⚠️ **Objectifs rarement atteints (< 30%) :**")
            for b in obj_issues:
                s = stats[b]
                r = s.get("obj_confirmed", 0) / s["total"] * 100
                lines.append(f"  • {b} : {r:.0f}% — cibles trop ambitieuses ?")

        if emq_issues:
            lines.append("\n📐 **EMQ faible (< 35%) — amplitude mal estimée :**")
            for b in emq_issues:
                s = stats[b]
                r = s.get("emq_good", 0) / s["total"] * 100
                lines.append(f"  • {b} : {r:.0f}%")

        # Verdict global basé sur la direction (métrique principale)
        if dir_rate >= 60:
            verdict = "✅ Dashboard utile"
        elif dir_rate >= 50:
            verdict = "⚠️ Dashboard marginal"
        else:
            verdict = "❌ Dashboard à recalibrer"
        lines.append(f"\n**{verdict}** (seuil direction : 60%)")
        lines.append(
            "_Direction ≠ Objectif ≠ EMQ — trois métriques indépendantes._"
        )

        return "\n".join(lines)
