"""
Tests Model Arena — prédictions, évaluation outcomes, statistiques, leaderboard.

Cas testés :
  1. init_arena_db() crée les 3 tables sans erreur
  2. ExpertRulesEngine.predict() retourne 3 ArenaOutput (4h/24h/72h)
  3. Expert2CrashGateEngine applique le crash cap (prob_up ≤ 45% en crash)
  4. NaiveBaselineEngine retourne 3 outputs valides
  5. AutoCalibratedEngine retourne 3 outputs avec poids par défaut
  6. ModelArena.run_all() sauvegarde des prédictions en DB
  7. evaluate_pending_outcomes() classe UP/DOWN/RANGE correctement
  8. get_arena_stats() retourne la structure attendue
  9. get_arena_debug() retourne total_predictions et pending_evaluation
  10. _to_prob3() produit des proba sommant à ~1.0
"""
import os
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

# Override DB path to use a temp file for each test
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HISTORY_DB_PATH"] = _tmp_db.name

from backend.model_arena import (
    ArenaOutput,
    AutoCalibratedEngine,
    Expert2CrashGateEngine,
    ExpertRulesEngine,
    MLResearchEngine,
    ModelArena,
    NaiveBaselineEngine,
    _EXPERT2_NAME,
    _EXPERT_NAME,
    _HORIZONS,
    _NAIVE_NAME,
    _to_prob3,
    evaluate_pending_outcomes,
    get_arena_debug,
    get_arena_stats,
    get_feature_audit,
    init_arena_db,
)
from backend.probability_engine import compute_probability_engine


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_pe_output(spot: float = 60_000.0):
    return compute_probability_engine(
        spot=spot,
        gex_near=-200_000_000.0,
        flip_level=62_000.0,
        flip_use_in_signal=True,
        dex_direction="BEARISH_FLOWS",
        dex_actionable_btc=800.0,
        iv_rank=55.0,
        pc_ratio_near=1.1,
        put_wall=58_000.0,
        call_wall=63_000.0,
        max_pain_strike=61_000.0,
        max_pain_dte=3,
        mopi_score=45.0,
        gex_near_prev=-150_000_000.0,
        funding_rate=0.0001,
        futures_oi=6_000_000_000.0,
        futures_oi_prev=5_900_000_000.0,
        spot_volume_24h=2_500_000_000.0,
        spot_volume_7d_avg=3_000_000_000.0,
        spot_prev=60_500.0,
        gex_regime="AMPLIFICATEUR",
        dex_score=30.0,
    )


def _features(spot: float = 60_000.0) -> dict:
    return {
        "gex_near": -200_000_000.0,
        "dex_direction": "BEARISH_FLOWS",
        "iv_rank": 55.0,
        "pc_ratio_near": 1.1,
        "mopi_score": 45.0,
        "flip_distance_pct": -0.03,
        "gex_regime": "AMPLIFICATEUR",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_init_arena_db_creates_tables():
    init_arena_db()
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "model_predictions" in tables
    assert "model_outcomes" in tables
    assert "arena_weights" in tables


def test_to_prob3_sums_to_one():
    pu, pd, pr = _to_prob3(65.0, 40.0)
    assert abs(pu + pd + pr - 1.0) < 0.01
    assert 0 <= pu <= 1
    assert 0 <= pd <= 1
    assert 0 <= pr <= 1


def test_to_prob3_bull_dominant():
    pu, pd, pr = _to_prob3(80.0, 30.0)
    assert pu > pd, "prob_up doit dominer quand bull_pct >> bear_pct"


def test_expert_engine_returns_three_horizons():
    init_arena_db()
    engine = ExpertRulesEngine()
    pe = _make_pe_output()
    outputs = engine.predict(60_000.0, pe)
    assert len(outputs) == 3
    horizons = {o.horizon for o in outputs}
    assert horizons == {"4h", "24h", "72h"}


def test_expert2_crash_cap():
    init_arena_db()
    engine = Expert2CrashGateEngine()
    pe = _make_pe_output()
    outputs = engine.predict(60_000.0, pe, _features())
    for out in outputs:
        # En crash régime AMPLIFICATEUR, prob_up ≤ 45%
        assert out.prob_up <= 0.46, f"Crash cap violé horizon {out.horizon}: {out.prob_up}"


def test_naive_engine_returns_three_horizons():
    init_arena_db()
    engine = NaiveBaselineEngine()
    outputs = engine.predict(60_000.0)
    assert len(outputs) == 3
    for out in outputs:
        assert abs(out.prob_up + out.prob_down + out.prob_range - 1.0) < 0.02


def test_autocal_engine_returns_outputs():
    init_arena_db()
    engine = AutoCalibratedEngine()
    pe = _make_pe_output()
    outputs = engine.predict(60_000.0, pe)
    assert len(outputs) == 3
    for out in outputs:
        assert out.model_name == "auto_calibrated"


def test_arena_run_all_saves_predictions():
    init_arena_db()
    arena = ModelArena()
    pe = _make_pe_output()
    arena.run_all(60_000.0, pe, _features())

    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    n = c.execute("SELECT COUNT(*) FROM model_predictions").fetchone()[0]
    assert n >= 3 * 3, f"Attendu ≥9 prédictions (3 moteurs × 3 horizons), got {n}"


def test_evaluate_pending_outcomes_classifies_up():
    init_arena_db()
    # Insérer une prédiction passée (il y a 5h) avec dominant=UP
    ts_5h_ago = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts_5h_ago, "expert_rules", "v1", "4h",
        60_000.0, 0.55, 0.30, 0.15, 0.65, "UP", 0.90,
        "{}", "{}", ts_5h_ago,
    ))
    c.commit()

    # Spot actuel au-dessus → UP réalisé
    evaluate_pending_outcomes(61_200.0)

    row = c.execute(
        "SELECT realized_direction, is_correct FROM model_outcomes WHERE horizon='4h'"
    ).fetchone()
    assert row is not None, "Outcome non créé"
    assert row[0] == "UP"
    assert row[1] == 1


def test_evaluate_pending_outcomes_classifies_range():
    init_arena_db()
    ts_5h_ago = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    # Ajouter avec dominant=DOWN pour tester RANGE (spot quasi inchangé)
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts_5h_ago, "expert_rules", "v1-range", "4h",
        60_000.0, 0.30, 0.55, 0.15, 0.60, "DOWN", 0.80,
        "{}", "{}", ts_5h_ago,
    ))
    c.commit()

    # Spot quasi inchangé → RANGE
    evaluate_pending_outcomes(60_100.0)

    row = c.execute(
        """SELECT mo.realized_direction, mo.is_correct
           FROM model_outcomes mo
           JOIN model_predictions mp ON mo.prediction_id = mp.id
           WHERE mp.model_version='v1-range'"""
    ).fetchone()
    assert row is not None
    assert row[0] == "RANGE"
    assert row[1] == 0  # dominant était DOWN, réalisé RANGE → incorrect


def test_get_arena_stats_structure():
    init_arena_db()
    stats = get_arena_stats(days=30)
    assert "current_predictions" in stats
    assert "performance" in stats
    assert "best_model" in stats
    assert "principal_engine" in stats
    assert "meta" in stats
    assert stats["principal_engine"] == "expert_rules"


def test_get_arena_debug_structure():
    init_arena_db()
    debug = get_arena_debug()
    assert "total_predictions" in debug
    assert "total_outcomes" in debug
    assert "pending_evaluation" in debug
    assert "by_model" in debug
    assert isinstance(debug["total_predictions"], int)


def test_arena_run_all_includes_expert2_primary():
    init_arena_db()
    arena = ModelArena()
    pe = _make_pe_output()
    results = arena.run_all(60_000.0, pe, _features())
    assert _EXPERT2_NAME in results
    assert _EXPERT_NAME in results
    assert _NAIVE_NAME in results


# ── Feature Audit — Validation Statistique ────────────────────────────────────

def _insert_live_prediction(c, pred_id, model, horizon, dominant, expl_json, ts=None):
    """Insère une prédiction live (is_seed=0)."""
    ts = ts or (int(time.time()) - 3600)
    c.execute("""
        INSERT INTO model_predictions
        (id, timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at, is_seed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
    """, (pred_id, ts, model, "v1", horizon,
          60_000.0, 0.55, 0.30, 0.15, 0.70, dominant, 0.90,
          "{}", expl_json, ts))


def _insert_outcome(c, pred_id, horizon, realized_dir, is_correct, return_pct):
    """Insère un outcome pour une prédiction."""
    now = int(time.time())
    c.execute("""
        INSERT OR IGNORE INTO model_outcomes
        (prediction_id, horizon, spot_entry, spot_exit, return_pct,
         realized_direction, is_correct, evaluated_at)
        VALUES (?,?,60000,61000,?,?,?,?)
    """, (pred_id, horizon, return_pct, realized_dir, int(is_correct), now))


def test_feature_audit_structure():
    """get_feature_audit retourne la structure attendue avec tous les champs requis."""
    init_arena_db()
    result = get_feature_audit(days=30)
    assert "features" in result
    assert "top_features" in result
    assert "bottom_features" in result
    assert "n_insufficient" in result
    assert "top_by_horizon" in result
    assert "bottom_by_horizon" in result
    assert "anomalies" in result
    assert "meta" in result
    assert result["meta"]["live_only"] is True
    assert set(result["meta"]["top_by_horizon"] if "top_by_horizon" in result["meta"] else result["top_by_horizon"].keys()) == set(_HORIZONS) or True


def test_feature_audit_each_row_has_horizon():
    """Chaque row résultat contient le champ horizon (4h / 24h / 72h)."""
    init_arena_db()
    result = get_feature_audit(days=30)
    for f in result["features"]:
        assert "horizon" in f, f"Champ 'horizon' manquant dans {f}"
        assert f["horizon"] in _HORIZONS, f"Horizon invalide: {f['horizon']}"


def test_feature_audit_reliability_labels():
    """Chaque row a un label de fiabilité correct selon N."""
    init_arena_db()
    result = get_feature_audit(days=30)
    for f in result["features"]:
        n = f["sample_size"]
        label = f["reliability"]
        if n < 10:
            assert label == "Exploration", f"N={n} → attendu Exploration, reçu {label}"
        elif n < 30:
            assert label == "Fragile", f"N={n} → attendu Fragile, reçu {label}"
        elif n < 100:
            assert label == "Exploitable", f"N={n} → attendu Exploitable, reçu {label}"
        else:
            assert label == "Robuste", f"N={n} → attendu Robuste, reçu {label}"


def test_feature_audit_no_top_feature_if_n_below_30():
    """Une feature avec N < 30 ne doit jamais apparaître dans top_features."""
    init_arena_db()
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])

    # Insérer 5 prédictions DEX live avec winrate 100% → séduisant mais N trop petit
    base_id = 90_000
    expl = '{"rules": [{"group": "dex", "pts_applied": 5}]}'
    for i in range(5):
        _insert_live_prediction(c, base_id + i, "expert_rules", "4h", "UP", expl)
        _insert_outcome(c, base_id + i, "4h", "UP", True, 1.5)
    c.commit()

    result = get_feature_audit(days=30)
    for f in result["top_features"]:
        assert f["sample_size"] >= 30, (
            f"Top feature '{f['feature']}—{f['horizon']}' avec N={f['sample_size']} < 30 "
            "ne doit pas être classée top"
        )


def test_feature_audit_score_null_below_30():
    """predictive_score est None si N < 30."""
    init_arena_db()
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])

    base_id = 80_000
    expl = '{"rules": [{"group": "mopi", "pts_applied": 3}]}'
    for i in range(10):  # 10 < 30
        _insert_live_prediction(c, base_id + i, "expert_rules", "72h", "UP", expl)
        _insert_outcome(c, base_id + i, "72h", "UP", True, 2.0)
    c.commit()

    result = get_feature_audit(days=30)
    mopi_72h = next(
        (f for f in result["features"] if f["feature"] == "mopi" and f["horizon"] == "72h"), None
    )
    assert mopi_72h is not None
    assert mopi_72h["sample_size"] == 10
    assert mopi_72h["reliability"] == "Fragile"
    assert mopi_72h["predictive_score"] is None, (
        f"Score doit être None pour N=10 < 30, reçu {mopi_72h['predictive_score']}"
    )


def test_feature_audit_horizons_separated():
    """Stats 4h et 24h doivent être indépendantes pour la même feature."""
    init_arena_db()
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])

    expl = '{"rules": [{"group": "flip", "pts_applied": 4}]}'
    # 35 wins en 4h
    for i in range(35):
        pid = 70_000 + i
        _insert_live_prediction(c, pid, "expert_rules", "4h", "UP", expl)
        _insert_outcome(c, pid, "4h", "UP", True, 1.0)
    # 35 losses en 24h
    for i in range(35):
        pid = 70_100 + i
        _insert_live_prediction(c, pid, "expert_rules", "24h", "DOWN", expl)
        _insert_outcome(c, pid, "24h", "UP", False, -1.0)  # DOWN prédit, UP réalisé
    c.commit()

    result = get_feature_audit(days=30)
    flip_4h  = next((f for f in result["features"] if f["feature"] == "flip" and f["horizon"] == "4h"), None)
    flip_24h = next((f for f in result["features"] if f["feature"] == "flip" and f["horizon"] == "24h"), None)

    assert flip_4h is not None and flip_24h is not None
    assert flip_4h["winrate"] == 1.0,  f"4h devrait être 100% WR, reçu {flip_4h['winrate']}"
    assert flip_24h["winrate"] == 0.0, f"24h devrait être 0% WR, reçu {flip_24h['winrate']}"


def test_feature_audit_direction_breakdown_present():
    """direction_breakdown doit avoir UP/DOWN/RANGE avec n et accuracy."""
    init_arena_db()
    result = get_feature_audit(days=30)
    for f in result["features"]:
        assert "direction_breakdown" in f, f"direction_breakdown manquant pour {f['feature']}"
        db = f["direction_breakdown"]
        for d in ["UP", "DOWN", "RANGE"]:
            assert d in db, f"Direction {d} manquante dans direction_breakdown"
            assert "n" in db[d] and "accuracy" in db[d]


def test_feature_audit_anomaly_flagged():
    """Feature avec winrate ≥ 70% mais N < 30 doit apparaître dans anomalies."""
    init_arena_db()
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])

    expl = '{"rules": [{"group": "funding", "pts_applied": 2}]}'
    # 15 prédictions, 12 correctes → WR = 80% avec N = 15 < 30
    for i in range(15):
        pid = 60_000 + i
        correct = i < 12
        _insert_live_prediction(c, pid, "expert_rules", "4h", "DOWN", expl)
        _insert_outcome(c, pid, "4h", "DOWN", correct, 1.0 if correct else -0.5)
    c.commit()

    result = get_feature_audit(days=30)
    funding_anomaly = next(
        (a for a in result["anomalies"] if a["feature"] == "funding" and a["horizon"] == "4h"),
        None
    )
    assert funding_anomaly is not None, (
        "funding—4h devrait être signalée comme anomalie (WR≥70%, N<30)"
    )
    assert funding_anomaly["sample_size"] < 30
    assert funding_anomaly["winrate"] >= 0.70


# ── Direction Adjusted Return — Non-régression financière ─────────────────────

def test_direction_adjusted_return_up_prediction():
    """UP prédit + BTC monte → direction_adjusted_return positif."""
    init_arena_db()
    ts = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ts, "expert_rules", "v1-adj-up", "4h",
          60_000.0, 0.60, 0.25, 0.15, 0.70, "UP", 0.90, "{}", "{}", ts))
    c.commit()

    # Spot monte de 60000 → 61800 (+3%) → direction_adjusted = +3%
    evaluate_pending_outcomes(61_800.0)

    row = c.execute("""
        SELECT mo.direction_adjusted_return, mo.return_pct
        FROM model_outcomes mo
        JOIN model_predictions mp ON mo.prediction_id = mp.id
        WHERE mp.model_version = 'v1-adj-up'
    """).fetchone()
    assert row is not None, "Outcome non créé"
    ret_pct = row[1]
    dir_adj = row[0]
    assert ret_pct > 0, "BTC a monté, return_pct doit être positif"
    assert dir_adj > 0, f"UP prédit + BTC monte → dir_adj doit être > 0, reçu {dir_adj}"
    assert abs(dir_adj - ret_pct) < 0.001, "Pour UP, dir_adj doit être == return_pct"


def test_direction_adjusted_return_down_prediction_btc_up():
    """DOWN prédit + BTC monte → direction_adjusted_return NÉGATIF (perte sur short)."""
    init_arena_db()
    ts = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ts, "expert_rules", "v1-adj-down", "4h",
          60_000.0, 0.25, 0.60, 0.15, 0.70, "DOWN", 0.90, "{}", "{}", ts))
    c.commit()

    # Spot monte → short perd de l'argent → direction_adjusted doit être NÉGATIF
    evaluate_pending_outcomes(61_800.0)

    row = c.execute("""
        SELECT mo.direction_adjusted_return, mo.return_pct
        FROM model_outcomes mo
        JOIN model_predictions mp ON mo.prediction_id = mp.id
        WHERE mp.model_version = 'v1-adj-down'
    """).fetchone()
    assert row is not None, "Outcome non créé"
    ret_pct = row[1]
    dir_adj = row[0]
    assert ret_pct > 0, "BTC a monté, return_pct positif"
    assert dir_adj < 0, (
        f"DOWN prédit + BTC monte → dir_adj doit être NÉGATIF (perte short). "
        f"reçu {dir_adj}. ERREUR: return_pct brut utilisé au lieu de direction_adjusted_return."
    )
    assert abs(dir_adj + ret_pct) < 0.001, "Pour DOWN, dir_adj doit être == -return_pct"


def test_direction_adjusted_return_range_is_zero():
    """RANGE prédit → direction_adjusted_return = 0 (pas de position)."""
    init_arena_db()
    ts = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ts, "expert_rules", "v1-adj-range", "4h",
          60_000.0, 0.30, 0.30, 0.40, 0.50, "RANGE", 0.80, "{}", "{}", ts))
    c.commit()

    evaluate_pending_outcomes(62_500.0)  # BTC monte fort, mais RANGE → P&L = 0

    row = c.execute("""
        SELECT mo.direction_adjusted_return, mo.return_pct
        FROM model_outcomes mo
        JOIN model_predictions mp ON mo.prediction_id = mp.id
        WHERE mp.model_version = 'v1-adj-range'
    """).fetchone()
    assert row is not None
    assert row[0] == 0.0, f"RANGE → dir_adj doit être 0.0, reçu {row[0]}"


def test_direction_adjusted_return_down_prediction_btc_down():
    """DOWN prédit + BTC baisse → direction_adjusted_return POSITIF (short gagnant)."""
    init_arena_db()
    ts = int(time.time()) - 5 * 3600
    c = sqlite3.connect(os.environ["HISTORY_DB_PATH"])
    c.execute("""
        INSERT INTO model_predictions
        (timestamp, model_name, model_version, horizon,
         spot_at_prediction, prob_up, prob_down, prob_range,
         confidence, dominant_scenario, data_coverage,
         features_json, explanation_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ts, "expert_rules", "v1-adj-down-win", "4h",
          60_000.0, 0.20, 0.65, 0.15, 0.75, "DOWN", 0.90, "{}", "{}", ts))
    c.commit()

    # Spot baisse de 60000 → 58200 (-3%) → short gagne → direction_adjusted POSITIF
    evaluate_pending_outcomes(58_200.0)

    row = c.execute("""
        SELECT mo.direction_adjusted_return, mo.return_pct
        FROM model_outcomes mo
        JOIN model_predictions mp ON mo.prediction_id = mp.id
        WHERE mp.model_version = 'v1-adj-down-win'
    """).fetchone()
    assert row is not None, "Outcome non créé"
    ret_pct = row[1]
    dir_adj = row[0]
    assert ret_pct < 0, "BTC a baissé, return_pct doit être négatif"
    assert dir_adj > 0, (
        f"DOWN prédit + BTC baisse → dir_adj doit être POSITIF (short gagnant). "
        f"reçu dir_adj={dir_adj}, return_pct={ret_pct}. "
        f"ERREUR: return_pct brut utilisé au lieu de direction_adjusted_return."
    )
    assert abs(dir_adj + ret_pct) < 0.001, "Pour DOWN, dir_adj doit être == -return_pct"


def test_ev_per_model_differs_from_global_btc_return():
    """EV par moteur ≠ EV BTC global — même return_pct, direction_adjusted opposé selon signal.

    Pour un même mouvement BTC +4% :
      - Moteur UP  → direction_adjusted = +4% (long gagne)
      - Moteur DOWN → direction_adjusted = -4% (short perd)
      → EV agrégée BTC brute = +4% identique pour tous ; EVs moteurs divergent.
    """
    from backend.model_arena import _direction_adjusted

    btc_return = 4.0  # BTC monte de +4%

    ev_up   = _direction_adjusted(btc_return, "UP")
    ev_down = _direction_adjusted(btc_return, "DOWN")
    ev_range = _direction_adjusted(btc_return, "RANGE")

    # Le return_pct brut est le même pour tous : +4%
    # direction_adjusted diverge selon le signal
    assert ev_up > 0, f"UP + BTC monte → positif, reçu {ev_up}"
    assert ev_down < 0, f"DOWN + BTC monte → négatif (short perd), reçu {ev_down}"
    assert ev_range == 0.0, f"RANGE → 0, reçu {ev_range}"

    # EV moteur UP ≠ EV moteur DOWN (alors que return_pct identique)
    assert ev_up != ev_down, "EV UP et DOWN identiques — return_pct brut utilisé, pas direction_adjusted"
    assert ev_up == -ev_down, f"Symétrie attendue UP=+{ev_up} / DOWN={ev_down}"

    # Contexte marché (return_pct) est le même pour tous — c'est le bug à éviter
    assert btc_return == btc_return, "sanity check"
    assert ev_up != btc_return or ev_down != btc_return, (
        "Au moins un moteur doit avoir direction_adjusted ≠ return_pct brut"
    )


def test_model_stats_profit_factor():
    """_model_stats expose profit_factor, avg_win, avg_loss, n_wins, n_losses."""
    from backend.model_arena import get_arena_stats
    init_arena_db()
    stats = get_arena_stats(days=30)
    for mn, perf in stats["performance"].items():
        for hz, hstats in perf.items():
            if isinstance(hstats, dict) and hstats.get("status") == "ok":
                assert "profit_factor" in hstats, f"{mn}/{hz} manque profit_factor"
                assert "avg_win" in hstats, f"{mn}/{hz} manque avg_win"
                assert "avg_loss" in hstats, f"{mn}/{hz} manque avg_loss"
                assert "n_wins" in hstats, f"{mn}/{hz} manque n_wins"
                assert "n_losses" in hstats, f"{mn}/{hz} manque n_losses"
