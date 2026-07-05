"""
Stats & Edge — Vue exécutive Validation réelle.

Agrège les données d'outcome tracking par event_type pour la section
"📊 Stats & Edge — Validation réelle" sur /test/option.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .event_store import (
    get_event_store,
    EVENT_LOG_PATH,
    PENDING_EVENTS_PATH,
    SILENT_LOG_PATH,
)
from .setup_tracker import get_setup_tracker

log = logging.getLogger(__name__)

# ── Tableau indicateurs : (event_type, indicator_group, family) ───────────────

INDICATOR_TABLE: List[Tuple[str, str, str]] = [
    # Direction
    ("dex_bullish",              "DEX",              "Direction"),
    ("dex_bearish",              "DEX",              "Direction"),
    ("mopi_cross",               "MOPI Cross",       "Direction"),
    ("mopi_bullish",             "MOPI",             "Direction"),
    ("mopi_bearish",             "MOPI",             "Direction"),
    ("dealer_buy_pressure",      "Signal Marché",    "Direction"),
    ("dealer_sell_pressure",     "Signal Marché",    "Direction"),
    # Volatilité / Magnitude
    ("gex_regime",               "GEX",              "Volatilité / Magnitude"),
    ("squeeze_bullish",          "Squeeze",          "Volatilité / Magnitude"),
    ("squeeze_bearish",          "Squeeze",          "Volatilité / Magnitude"),
    ("gravity_explosive",        "Gravity Explosive","Volatilité / Magnitude"),
    # Niveaux
    ("wall_rejection",           "Walls",            "Niveaux"),
    ("wall_breakout",            "Walls",            "Niveaux"),
    ("wall_rejection_candidate", "Walls Candidats",  "Niveaux"),
    ("wall_breakout_candidate",  "Walls Candidats",  "Niveaux"),
    ("max_pain_pull",            "Max Pain",         "Niveaux"),
    ("max_pain_shift",           "Max Pain",         "Niveaux"),
    ("gravity_magnet",           "Gravity Magnet",   "Niveaux"),
]

# event_types dont les observations viennent uniquement du log silencieux (pas de pipeline outcome 72h)
# Ces types affichent un compteur de signaux bloqués/observés mais jamais de score de winrate.
# dex_bullish/dex_bearish ont migré vers log_observation() → pipeline 72h (Phase 1C)
_SILENT_ONLY_ETYPES: set = {
    "wall_rejection_candidate",
    "wall_breakout_candidate",
}

# Labels UX pour les event_types dont le nom technique est trompeur
_DISPLAY_LABELS: Dict[str, str] = {
    "dealer_buy_pressure":      "Flip DEX → Haussier",
    "dealer_sell_pressure":     "Flip DEX → Baissier",
    "mopi_bullish":             "MOPI Extrême Haussier (>75)",
    "mopi_bearish":             "MOPI Extrême Baissier (<25)",
    "gravity_magnet":           "Gravity Magnet (score ≥65)",
    "wall_rejection_candidate": "Walls — rejets détectés mais bloqués",
    "wall_breakout_candidate":  "Walls — breakouts détectés mais bloqués",
}

# Raison explicite affichée quand un indicateur n'a aucune observation
_EMPTY_REASONS: Dict[str, str] = {
    "dex_bullish":              "Aucun régime DEX haussier observé sur la période",
    "dex_bearish":              "Aucun régime DEX baissier observé sur la période",
    "mopi_bullish":             "Aucun MOPI extrême haussier >75 observé",
    "mopi_bearish":             "Aucun MOPI extrême baissier <25 observé",
    "mopi_cross":               "Aucun crossing MOPI 55/45 détecté",
    "dealer_buy_pressure":      "Aucun flip DEX haussier détecté",
    "dealer_sell_pressure":     "Aucun flip DEX baissier détecté",
    "gex_regime":               "Aucun changement de régime GEX détecté",
    "squeeze_bullish":          "Aucun squeeze critique haussier score≥80 observé",
    "squeeze_bearish":          "Aucun squeeze critique baissier score≥80 observé",
    "gravity_explosive":        "Aucune zone explosive détectée sur la période",
    "gravity_magnet":           "Aucun magnet Gravity actif (score <65) observé",
    "wall_rejection":           "Aucune alerte envoyée ; voir wall_rejection_candidate pour les signaux bloqués",
    "wall_breakout":            "Aucune alerte envoyée ; voir wall_breakout_candidate pour les signaux bloqués",
    "wall_rejection_candidate": "Aucun rejet détecté mais bloqué par conviction/distance/dormant",
    "wall_breakout_candidate":  "Aucun breakout détecté mais bloqué par conviction/distance/dormant",
    "max_pain_pull":            "Aucune attraction max pain ≤5% détectée",
    "max_pain_shift":           "Aucun déplacement max pain ≥1% détecté",
}

# Mapping event_type pipeline → event_types dans le silent log (pour afficher n_silent_raw)
# Permet de montrer : "55 observations brutes → 1 setup unique" (transparence collecte)
_PIPELINE_TO_SILENT_RAW: Dict[str, List[str]] = {
    "gravity_explosive": ["gravity_explosive_down", "gravity_explosive_up", "gravity_explosive_symmetric"],
    "max_pain_pull":     ["max_pain_near_pull"],
    "gravity_magnet":    ["gravity_magnet_active"],
}

# event_types qui doivent avoir un pipeline (PendingEvent → +4h/+24h/+72h) pour afficher un score
# Si un type est uniquement dans le silent log, il ne doit jamais afficher de score
_HAS_PIPELINE: set = {
    "dex_bullish", "dex_bearish",
    "mopi_cross", "mopi_bullish", "mopi_bearish",
    "dealer_buy_pressure", "dealer_sell_pressure",
    "gex_regime",
    "squeeze_bullish", "squeeze_bearish",
    "gravity_explosive",
    "wall_rejection", "wall_breakout",
    "max_pain_pull", "max_pain_shift",
    "gravity_magnet",
}

# Seuils de confiance (N total)
_THRESHOLDS = [
    (0,   10,  "insuffisant",   "Insuffisant"),
    (10,  30,  "préliminaire",  "Préliminaire"),
    (30,  100, "exploitable",   "Exploitable"),
    (100, None, "robuste",       "Robuste"),
]

# N minimum pour afficher un score
_MIN_SCORE_N = 10

# Cibles globales Phase Observation
_TARGET_GLOBAL     = 30   # finalisés pour recalibration
_TARGET_PER_CAT    = 10   # outcomes +72h par groupe indicateur


def _confidence(n: int) -> Tuple[str, str]:
    for lo, hi, key, label in _THRESHOLDS:
        if n >= lo and (hi is None or n < hi):
            return key, label
    return "robuste", "Robuste"


def _read_jsonl_since(path: Path, days: int) -> List[dict]:
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
    entries: List[dict] = []
    if not path.exists():
        return entries
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_raw = rec.get("ts")
                    if ts_raw:
                        ts = datetime.fromisoformat(ts_raw)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            entries.append(rec)
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"[stats_edge] read {path.name}: {e}")
    return entries


def _first_event_ts() -> Optional[datetime]:
    """Timestamp du premier event connu (finalisé ou pending)."""
    first: Optional[datetime] = None

    if EVENT_LOG_PATH.exists():
        try:
            with open(EVENT_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts_raw = json.loads(line).get("ts", "")
                        ts = datetime.fromisoformat(ts_raw)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if first is None or ts < first:
                            first = ts
                    except Exception:
                        pass
        except Exception:
            pass

    if PENDING_EVENTS_PATH.exists():
        try:
            with open(PENDING_EVENTS_PATH) as f:
                data = json.load(f)
            for item in data:
                ts_raw = item.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if first is None or ts < first:
                        first = ts
                except Exception:
                    pass
        except Exception:
            pass

    return first


def _empty_etype_stat() -> dict:
    return {
        "finalized": 0, "pending": 0, "n_total": 0,
        "hit": 0, "invalidated": 0,
        "n_4h": 0, "n_24h": 0, "n_72h": 0,
        "o4": [], "o24": [], "o72": [],
        "silent_only": False,
        # Setup unique tracking (setup_id présent uniquement sur les events post-fix)
        "setup_ids_finalized": set(),   # setups ayant complété 72h (échantillons statistiques)
        "setup_ids_pending":   set(),   # setups en attente d'outcome
        "setup_hits":          0,       # hits parmi les setups finalisés
    }


def compute_stats_edge(days: int = 30) -> dict:
    now = datetime.now(timezone.utc)

    # ── 0. Setup tracker counts (compression, durée, couverture) ─────────────
    try:
        setup_counts = get_setup_tracker().get_setup_counts(days=days)
    except Exception as e:
        log.warning(f"[stats_edge] setup_counts error: {e}")
        setup_counts = {}

    # ── 1. Finalized events ───────────────────────────────────────────────────
    finalized_all = _read_jsonl_since(EVENT_LOG_PATH, days=days)

    # ── 2. Pending events ─────────────────────────────────────────────────────
    pending_all: List[dict] = []
    if PENDING_EVENTS_PATH.exists():
        try:
            with open(PENDING_EVENTS_PATH) as f:
                data = json.load(f)
            cutoff = now - timedelta(days=days)
            for item in data:
                ts_raw = item.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        pending_all.append(item)
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[stats_edge] load pending: {e}")

    # ── 3. Silent events ──────────────────────────────────────────────────────
    silent_all = _read_jsonl_since(SILENT_LOG_PATH, days=days)

    # ── 4. Comptages globaux ──────────────────────────────────────────────────
    total_pending   = len(pending_all)
    total_finalized = len(finalized_all)
    total_obs       = total_pending + total_finalized

    # Comptage brut depuis le silent log (avant dedup) par event_type
    # Utilisé pour n_silent_raw : afficher "55 observations brutes → 1 setup unique"
    silent_raw_by_etype: Dict[str, int] = {}
    for rec in silent_all:
        etype = rec.get("event_type", "")
        if etype:
            silent_raw_by_etype[etype] = silent_raw_by_etype.get(etype, 0) + 1

    # Outcomes partiels depuis pending
    out_4h  = sum(1 for p in pending_all if p.get("checked_4h"))  + total_finalized
    out_24h = sum(1 for p in pending_all if p.get("checked_24h")) + total_finalized
    out_72h = total_finalized

    # ── 5. Phase — basée sur outcomes 72h finalisés (jamais sur observations brutes) ──
    if out_72h < 10:
        phase = "COLLECTE"
        phase_label = "Données insuffisantes pour conclure scientifiquement."
    elif out_72h < 30:
        phase = "PRÉLIMINAIRE"
        phase_label = "Données préliminaires — scores indicatifs seulement."
    elif out_72h < 100:
        phase = "EXPLOITABLE"
        phase_label = "Données exploitables — scores fiables."
    else:
        phase = "ROBUSTE"
        phase_label = "Données robustes — scores statistiquement significatifs."

    # ── 6. Stats par event_type (finalized) ───────────────────────────────────
    etype_stats: Dict[str, dict] = {}

    for rec in finalized_all:
        etype = rec.get("event_type", "")
        if not etype:
            continue
        if etype not in etype_stats:
            etype_stats[etype] = _empty_etype_stat()
        s = etype_stats[etype]
        s["finalized"] += 1
        s["n_total"]   += 1
        if rec.get("hit_target"):    s["hit"]        += 1
        if rec.get("invalidated"):   s["invalidated"] += 1
        o4  = rec.get("outcome_4h")
        o24 = rec.get("outcome_24h")
        o72 = rec.get("outcome_72h")
        if o4  is not None: s["n_4h"]  += 1; s["o4"].append(o4)
        if o24 is not None: s["n_24h"] += 1; s["o24"].append(o24)
        if o72 is not None: s["n_72h"] += 1; s["o72"].append(o72)
        # Setup tracking : seuls les records avec setup_id comptent comme échantillons statistiques
        sid = rec.get("setup_id")
        if sid:
            s["setup_ids_finalized"].add(sid)
            if rec.get("hit_target") is True:
                s["setup_hits"] += 1

    # Stats par event_type (pending — outcomes partiels)
    for rec in pending_all:
        etype = rec.get("event_type", "")
        if not etype:
            continue
        if etype not in etype_stats:
            etype_stats[etype] = _empty_etype_stat()
        s = etype_stats[etype]
        s["pending"] += 1
        s["n_total"] += 1
        if rec.get("checked_4h") and rec.get("outcome_4h") is not None:
            s["n_4h"] += 1
            s["o4"].append(rec["outcome_4h"])
        if rec.get("checked_24h") and rec.get("outcome_24h") is not None:
            s["n_24h"] += 1
            s["o24"].append(rec["outcome_24h"])
        sid = rec.get("setup_id")
        if sid:
            s["setup_ids_pending"].add(sid)

    # Comptage des event_types silent_only depuis le silent log
    for rec in silent_all:
        etype = rec.get("event_type", "")
        if etype not in _SILENT_ONLY_ETYPES:
            continue
        if etype not in etype_stats:
            etype_stats[etype] = _empty_etype_stat()
            etype_stats[etype]["silent_only"] = True
        etype_stats[etype]["n_total"] += 1

    # ── 7. Progress par indicator_group (72h outcomes) ────────────────────────
    cat_72h: Dict[str, int] = {}
    seen_groups = []
    for _, ind_group, _ in INDICATOR_TABLE:
        if ind_group not in seen_groups:
            seen_groups.append(ind_group)
            cat_72h[ind_group] = 0

    for rec in finalized_all:
        etype = rec.get("event_type", "")
        for cfg_etype, ind_group, _ in INDICATOR_TABLE:
            if cfg_etype == etype:
                cat_72h[ind_group] = cat_72h.get(ind_group, 0) + 1
                break

    # ── 8. Construire la liste indicateurs pour le frontend ───────────────────
    indicators = []
    seen_etypes = set()

    for cfg_etype, ind_group, family in INDICATOR_TABLE:
        if cfg_etype in seen_etypes:
            continue
        seen_etypes.add(cfg_etype)

        s = etype_stats.get(cfg_etype, _empty_etype_stat())
        n_total    = s["n_total"]
        n_fin      = s["finalized"]
        n_pend     = s["pending"]
        n_4h       = s["n_4h"]
        n_24h      = s["n_24h"]
        n_72h      = s["n_72h"]
        hit        = s["hit"]
        silent_only = s.get("silent_only", False)

        # ── Setups uniques (échantillons statistiques réels) ─────────────────
        # Seuls les PendingEvents créés APRÈS le fix setup_tracker ont setup_id.
        # Legacy events (sans setup_id) contribuent à n_total mais pas à n_setups_finalized.
        n_setups_finalized = len(s["setup_ids_finalized"])
        n_setups_pending   = len(s["setup_ids_pending"])
        n_setups           = n_setups_finalized + n_setups_pending

        # ── Confiance — basée sur setups finalisés (N statistique réel) ──────
        # Si aucun setup finalisé → insuffisant (reset honnête, pas d'héritage inflaté)
        conf_key, conf_label = _confidence(n_setups_finalized)
        confidence_basis = "setup"

        # ── Score / winrate — basé sur setup_hits / n_setups_finalized ───────
        score       = None
        winrate_pct = None
        if not silent_only and n_setups_finalized >= _MIN_SCORE_N:
            winrate_pct = round(s["setup_hits"] / n_setups_finalized * 100, 1)
            score       = round(winrate_pct)

        # ── Données brutes pour les horizons (affichage uniquement) ──────────
        if n_72h > 0:
            n_for_maturity = n_72h
        elif n_24h > 0:
            n_for_maturity = n_24h
        else:
            n_for_maturity = n_4h

        o4_list  = s.get("o4", [])
        o24_list = s.get("o24", [])
        o72_list = s.get("o72", [])
        avg_4h   = round(sum(o4_list)  / len(o4_list),  2) if o4_list  else None
        avg_24h  = round(sum(o24_list) / len(o24_list), 2) if o24_list else None
        avg_72h  = round(sum(o72_list) / len(o72_list), 2) if o72_list else None

        # ── Observations brutes depuis le silent log (avant dedup) ───────────
        # Permet d'afficher "55 obs brutes → 1 setup" pour gravity/max_pain
        silent_etypes_for_pipeline = _PIPELINE_TO_SILENT_RAW.get(cfg_etype, [])
        n_silent_raw = sum(silent_raw_by_etype.get(se, 0) for se in silent_etypes_for_pipeline)
        # Pour les types silent_only, n_silent_raw = leur propre comptage dans silent log
        if silent_only:
            n_silent_raw = silent_raw_by_etype.get(cfg_etype, 0)

        # ── Status lisible ────────────────────────────────────────────────────
        has_pipeline = cfg_etype in _HAS_PIPELINE
        if n_total == 0 and n_silent_raw == 0:
            status      = "Aucune observation collectée"
            empty_reason = _EMPTY_REASONS.get(cfg_etype, "Aucune observation collectée")
        elif silent_only and n_setups_finalized == 0:
            status      = f"Signaux détectés bloqués — {n_total} signal(s) sur la période"
            empty_reason = None
        elif conf_key == "insuffisant":
            status = (
                f"Collecte — {n_setups_finalized}/{_MIN_SCORE_N} setups finalisés"
                if n_setups_finalized > 0
                else f"Collecte — {n_total} obs / 0 setup finalisé"
            )
            empty_reason = None
        elif conf_key == "préliminaire":
            status       = f"Préliminaire — {n_setups_finalized} setups finalisés"
            empty_reason = None
        elif conf_key == "exploitable":
            status       = f"Exploitable — {n_setups_finalized} setups"
            empty_reason = None
        else:
            status       = f"Robuste — {n_setups_finalized} setups"
            empty_reason = None

        # ── Setup tracker enrichment ──────────────────────────────────────────
        sc = setup_counts.get(cfg_etype, {})
        compression_ratio       = sc.get("compression_ratio", None)
        avg_setup_duration_h    = sc.get("avg_setup_duration_hours", None)
        n_setups_tracker_total  = sc.get("n_setups_total", 0)
        bullish_setups          = sc.get("bullish_setups", 0)
        bearish_setups          = sc.get("bearish_setups", 0)
        neutral_setups          = sc.get("neutral_setups", 0)

        indicators.append({
            "event_type":          cfg_etype,
            "display_label":       _DISPLAY_LABELS.get(cfg_etype, cfg_etype),
            "indicator_group":     ind_group,
            "family":              family,
            # Volume collecte — distinguer brut / setups / finalisés
            "observations":        n_total,       # pipeline events (pending + finalized)
            "n_silent_raw":        n_silent_raw,  # entrées brutes dans silent log (avant dedup)
            "n_total":             n_total,
            "pending":             n_pend,
            "finalized":           n_fin,
            # Setups uniques (N statistique réel)
            "n_setups":            n_setups,            # setups finalisés + pending
            "n_setups_finalized":  n_setups_finalized,  # setups avec outcome 72h complet
            "n_setups_pending":    n_setups_pending,    # setups en cours (pas encore 72h)
            # Métriques qualité setup (compression, durée, couverture)
            "compression_ratio":         compression_ratio,        # obs / setup (spam détecteur)
            "avg_setup_duration_hours":  avg_setup_duration_h,     # durée moy. en heures
            "bullish_setups":            bullish_setups,           # setups direction haussière
            "bearish_setups":            bearish_setups,           # setups direction baissière
            "neutral_setups":            neutral_setups,           # setups sans direction
            # Outcomes par horizon (affichage)
            "outcomes_validated":  n_for_maturity,
            "confidence_basis":    confidence_basis,
            "outcome_4h":          n_4h,
            "outcome_24h":         n_24h,
            "outcome_72h":         n_72h,
            "avg_outcome_4h":      avg_4h,
            "avg_outcome_24h":     avg_24h,
            "avg_outcome_72h":     avg_72h,
            # Score statistique (setups finalisés uniquement — jamais observations brutes)
            "score":               score,
            "winrate_pct":         winrate_pct,
            "confidence":          conf_key,
            "confidence_label":    conf_label,
            "status":              status,
            "silent_only":         silent_only,
            "has_pipeline":        has_pipeline,
            "empty_reason":        empty_reason,
        })

    # ── 9. Période de collecte ────────────────────────────────────────────────
    first_ts = _first_event_ts()
    collection_days = max(1, (now - first_ts).days) if first_ts else 0

    # ── 10. Observations récentes (mini log) ──────────────────────────────────
    _all_obs: List[dict] = []

    for rec in finalized_all:
        ts_raw = rec.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            _all_obs.append({"ts": ts, "event_type": rec.get("event_type", "?"), "status": "finalisé"})
        except Exception:
            pass

    for rec in pending_all:
        ts_raw = rec.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if rec.get("checked_4h"):
                st = "pending +24h"
            else:
                st = "pending +4h"
            _all_obs.append({"ts": ts, "event_type": rec.get("event_type", "?"), "status": st})
        except Exception:
            pass

    _all_obs.sort(key=lambda x: x["ts"], reverse=True)
    recent_observations = [
        {
            "ts":         o["ts"].strftime("%H:%M"),
            "event_type": o["event_type"],
            "status":     o["status"],
        }
        for o in _all_obs[:10]
    ]

    last_obs_minutes_ago: Optional[int] = None
    if _all_obs:
        delta = now - _all_obs[0]["ts"]
        last_obs_minutes_ago = max(0, int(delta.total_seconds() // 60))

    # ── 11. Prochain outcome +4h attendu ─────────────────────────────────────
    next_4h_minutes: Optional[int] = None
    for rec in pending_all:
        if rec.get("checked_4h"):
            continue
        ts_raw = rec.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            target = ts + timedelta(hours=4)
            remaining = (target - now).total_seconds() / 60
            if remaining > 0:
                if next_4h_minutes is None or remaining < next_4h_minutes:
                    next_4h_minutes = int(remaining)
        except Exception:
            pass

    # ── 12. Réponse finale ────────────────────────────────────────────────────
    progress_pct = min(100, round(total_finalized / _TARGET_GLOBAL * 100))

    return {
        "generated_at":          now.isoformat(),
        "phase":                 phase,
        "phase_label":           phase_label,
        "totals": {
            "observations": total_obs,
            "pending":      total_pending,
            "finalized":    total_finalized,
        },
        "outcomes": {
            "validated_4h":  out_4h,
            "validated_24h": out_24h,
            "validated_72h": out_72h,
        },
        "progress": {
            "global": {
                "current": total_finalized,
                "target":  _TARGET_GLOBAL,
                "pct":     progress_pct,
            },
            "per_category": {
                "target":      _TARGET_PER_CAT,
                "by_category": {
                    group: {
                        "current": cat_72h.get(group, 0),
                        "target":  _TARGET_PER_CAT,
                    }
                    for group in seen_groups
                    if group not in _SILENT_ONLY_ETYPES
                },
            },
        },
        "collection_period_days":  collection_days,
        "last_update":             now.strftime("%H:%M UTC"),
        "indicators":              indicators,
        "data_sufficient":         total_finalized >= _MIN_SCORE_N,
        "recent_observations":     recent_observations,
        "last_obs_minutes_ago":    last_obs_minutes_ago,
        "next_4h_minutes":         next_4h_minutes,
        "methodology": {
            "description": "Les scores sont en phase de collecte.",
            "families": {
                "Direction":              "Le prix va-t-il dans le bon sens ?",
                "Volatilité / Magnitude": "Le mouvement est-il plus violent que la normale ?",
                "Niveaux":                "Le prix touche-t-il, rejette-t-il ou casse-t-il le niveau ?",
            },
            "thresholds": {
                "minimum":     _MIN_SCORE_N,
                "preliminary": 30,
                "robust":      100,
            },
            "note": "Les règles actuelles sont provisoires pour accumuler les outcomes. La calibration finale se fera après N suffisant.",
        },
    }
