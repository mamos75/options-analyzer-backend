"""
Backtest Signal Mamos — Validation statistique des composantes.

Pour chaque snapshot T dans la DB :
  1. Classifier le signal (MOPI > 65, GEX régime, DEX direction, etc.)
  2. Trouver btc_price à T+24h, T+72h, T+7j
  3. Mesurer performance, winrate, drawdown

Signal Mamos V1 : combinaison pondérée MOPI + DEX + Gravity + Flip
Objectif : passer d'une formule intuitive à une formule validée statistiquement.
"""

import random
import sqlite3
import time
import math
import os
from typing import List, Dict, Optional, Tuple

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

HORIZONS = {
    "4h":  14400,
    "24h": 86400,
    "72h": 259200,
    "7j":  604800,
}

# Seuils de signification statistique minimaux
MIN_N_STATS     = 15   # en dessous : stats peu fiables
MIN_N_VALID     = 5    # en dessous : ne pas afficher
MIN_N_GRADE     = 30   # en dessous : pas de grade marketing — "Données insuffisantes"
MIN_N_CONFIDENT = 50   # confiance maximale atteinte


def compute_confidence(n: int, ev: float, std: float) -> Tuple[int, str]:
    """Score de confiance 0-100 + label Low/Medium/High.
    N est le facteur primaire (0-70 pts) — un beau CV sur 4 trades ne vaut rien.
    Stabilité EV est secondaire (0-30 pts).
    """
    # Composante taille d'échantillon (0-70 pts) — poids primaire
    if n >= 100:
        n_score = 70
    elif n >= MIN_N_CONFIDENT:   # 50
        n_score = 60
    elif n >= MIN_N_GRADE:       # 30
        n_score = 50
    elif n >= MIN_N_STATS:       # 15
        n_score = 30
    elif n >= MIN_N_VALID:       # 5
        n_score = 10
    else:
        n_score = 0              # N < 5 → 0, impossible d'être > Low

    # Composante stabilité EV (0-30 pts) : coefficient de variation = std / |ev|
    if ev <= 0 or std <= 0:
        stab_score = 0
    else:
        cv = std / abs(ev)
        if cv < 1.0:
            stab_score = 30
        elif cv < 2.0:
            stab_score = 20
        elif cv < 4.0:
            stab_score = 10
        else:
            stab_score = 0

    score = min(100, n_score + stab_score)
    if score >= 70:
        label = "High"
    elif score >= 40:
        label = "Medium"
    else:
        label = "Low"
    return score, label


def _bootstrap_p_value(perfs: List[float], n_bootstrap: int = 500) -> float:
    """P-value bootstrap (sign-permutation test, H0 : EV ≤ 0).
    Mesure la fraction de bootstraps avec EV aléatoire ≥ EV réelle.
    p_value faible → signal robuste (ex: p=0.02 → 2% de chance que c'est du hasard).
    """
    n = len(perfs)
    if n < 5:
        return 1.0
    real_ev = sum(perfs) / n
    count_geq = 0
    for _ in range(n_bootstrap):
        boot_ev = sum(p * (1.0 if random.random() > 0.5 else -1.0) for p in perfs) / n
        if boot_ev >= real_ev:
            count_geq += 1
    return round(count_geq / n_bootstrap, 3)


def grade_signal(ev: float, n: int) -> str:
    """Grade final basé sur EV ET taille d'échantillon minimale.
    Une statistique sans taille d'échantillon est une opinion déguisée en donnée.
    """
    if n < MIN_N_GRADE:
        return "INSUFFICIENT DATA"   # Jamais de grade avant N ≥ 30
    if ev > 4.0 and n >= MIN_N_CONFIDENT:
        return "A"           # A = EV > 4% ET N ≥ 50
    if ev > 2.0:
        return "B"           # B = EV > 2% ET N ≥ 30
    if ev > 0.0:
        return "C"
    if ev > -2.0:
        return "D"
    return "F"


# ─── Normalisation des composantes brutes de la DB ───────────────────────────

def _gex_to_score(gex_usd: float) -> float:
    """GEX USD → 0-100 bullish (cap calibré sur l'ordre de grandeur réel BTC ~1-5B)."""
    cap = 5_000_000_000
    return max(0.0, min(100.0, (gex_usd / cap + 1) / 2 * 100))


def _dex_to_score(dex_btc: float) -> float:
    """DEX net_delta BTC → 0-100 bullish.
    dex < -500 = BULLISH_FLOWS (dealers achètent en hedging) → score élevé.
    dex > 500  = BEARISH_FLOWS (dealers vendent en hedging) → score faible.
    """
    cap = 20_000.0
    return max(0.0, min(100.0, (-dex_btc / cap + 1) / 2 * 100))


def _pc_ratio_to_score(pc: float) -> float:
    """PCR → 0-100 bullish (contrarian : PCR > 1.5 = trop de puts = bullish)."""
    if pc >= 1.5:
        return min(100.0, 70 + (pc - 1.5) * 20)
    elif pc <= 0.5:
        return max(0.0, 30 - (0.5 - pc) * 20)
    return 30 + (pc - 0.5) / 1.0 * 40


def _gravity_to_score(max_pain: float, btc_price: float) -> float:
    """Max Pain > BTC = aimant haussier → score bullish.
    Différence de 5% → score ≈ 65. Différence nulle → score = 50.
    """
    if btc_price <= 0 or max_pain <= 0:
        return 50.0
    diff_pct = (max_pain - btc_price) / btc_price
    return max(0.0, min(100.0, 50 + diff_pct * 300))


def _flip_to_score(flip_level: float, btc_price: float) -> float:
    """BTC au-dessus du flip level (régime stabilisant déjà acquis) → bullish.
    BTC sous le flip level → bearish (dealers amplificateurs).
    """
    if btc_price <= 0 or flip_level <= 0:
        return 50.0
    diff_pct = (btc_price - flip_level) / btc_price
    return max(0.0, min(100.0, 50 + diff_pct * 500))


# ─── Signal Mamos V1 ─────────────────────────────────────────────────────────

WEIGHTS_V1 = {
    "mopi":    0.35,  # score composite options déjà validé
    "dex":     0.20,  # dealer delta → direction hedging
    "gravity": 0.15,  # max pain aimant
    "flip":    0.15,  # régime dealers
    "pcr":     0.15,  # put/call contrarian
}


def _is_crash_regime_v1(row: Dict, dex_score: float) -> bool:
    """V2 — Crash Regime Gate pour Signal V1.

    Cas réel 30 mai 2025 : 30 signaux HAUSSIER → 0% winrate.
    DEX = +9,147 BTC (score ~27%) + GEX AMPLIFICATEUR.

    Si crash régime actif : score V1 HAUSSIER bloqué à 55% max (= NEUTRE).
    """
    gex = float(row.get("gex") or 0)
    gex_amplificateur = gex < -5_000_000  # GEX négatif = régime amplificateur
    dex_extreme_baissier = dex_score <= 35.0
    dex_bearish_flows = float(row.get("dex") or 0) > 500  # >500 BTC net = bearish flows
    return gex_amplificateur and dex_extreme_baissier and dex_bearish_flows


def compute_signal_mamos_v1(row: Dict) -> Tuple[str, float]:
    """Retourne (signal: HAUSSIER/BAISSIER/NEUTRE, score_v1: 0-100).

    V2 : Crash Regime Gate — si DEX extrêmement baissier + GEX AMPLIFICATEUR,
    le signal HAUSSIER est bloqué à 55% max.
    Règle : Max Pain / Gravity ne sont pas des ordres donnés au marché.
    Ce sont des aimants conditionnels. En régime de crash, les flux dealers dominent.
    """
    mopi_score = float(row.get("mopi") or 50)
    dex_score = _dex_to_score(float(row.get("dex") or 0))
    gravity_score = _gravity_to_score(
        float(row.get("max_pain") or 0),
        float(row.get("btc_price") or 1),
    )
    flip_score = _flip_to_score(
        float(row.get("flip_level") or 0),
        float(row.get("btc_price") or 1),
    )
    # Préférer pc_ratio_near si disponible (pression immédiate)
    pc = float(row.get("pc_ratio_near") or row.get("pc_ratio") or 1.0)
    pcr_score = _pc_ratio_to_score(pc)

    v1 = (
        WEIGHTS_V1["mopi"]    * mopi_score
        + WEIGHTS_V1["dex"]   * dex_score
        + WEIGHTS_V1["gravity"] * gravity_score
        + WEIGHTS_V1["flip"]  * flip_score
        + WEIGHTS_V1["pcr"]   * pcr_score
    )
    v1 = round(max(0.0, min(100.0, v1)), 1)

    # V2 — Crash Regime Gate : bloquer HAUSSIER si flux dealers extrêmes baissiers
    if v1 >= 60 and _is_crash_regime_v1(row, dex_score):
        v1 = 55.0  # forcer NEUTRE — Max Pain ignoré en crash régime
        return "NEUTRE", v1

    if v1 >= 60:
        return "HAUSSIER", v1
    elif v1 <= 40:
        return "BAISSIER", v1
    return "NEUTRE", v1


# ─── Stats ───────────────────────────────────────────────────────────────────

def _perf(price_now: float, price_future: float) -> float:
    """Retourne la performance en % (positif = hausse BTC)."""
    if price_now <= 0:
        return 0.0
    return (price_future - price_now) / price_now * 100


def _compute_stats(perfs: List[float], run_bootstrap: bool = False) -> Dict:
    """Stats + EV sur une liste de performances en % (déjà orientées dans le sens du signal)."""
    n = len(perfs)
    if n < MIN_N_VALID:
        return {"n": n, "insufficient": True}
    perfs_sorted = sorted(perfs)
    mean = sum(perfs) / n
    winners = [p for p in perfs if p > 0]
    losers = [p for p in perfs if p <= 0]
    winrate_f = len(winners) / n
    gain_moyen = round(sum(winners) / len(winners), 2) if winners else 0.0
    perte_moyenne = round(abs(sum(losers) / len(losers)), 2) if losers else 0.0
    ev = round(winrate_f * gain_moyen - (1 - winrate_f) * perte_moyenne, 2)
    max_dd = min(perfs)
    best = max(perfs)
    median = perfs_sorted[n // 2]
    variance = sum((p - mean) ** 2 for p in perfs) / n
    std = math.sqrt(variance)
    conf_score, conf_label = compute_confidence(n, ev, std)
    grade = grade_signal(ev, n)
    p_value = _bootstrap_p_value(perfs) if run_bootstrap and n >= MIN_N_VALID else None
    return {
        "n": n,
        "sample_size": n,
        "perf_moy": round(mean, 2),
        "rendement_moyen": round(mean, 2),
        "rendement_median": round(median, 2),
        "winrate": round(winrate_f * 100, 1),
        "gain_moyen": gain_moyen,
        "perte_moyenne": perte_moyenne,
        "ev": ev,
        "grade": grade,
        # MAE/MFE approximés sur données snapshots (pas intraday)
        "mae_proxy": round(max_dd, 2),     # max adverse excursion ≈ pire retour observé
        "mfe_proxy": round(best, 2),       # max favorable excursion ≈ meilleur retour observé
        "drawdown_moyen": round(sum(losers) / len(losers), 2) if losers else 0.0,
        "max_dd": round(max_dd, 2),
        "best": round(best, 2),
        "median": round(median, 2),
        "std": round(std, 2),
        "confidence_score": conf_score,
        "confidence_label": conf_label,
        "bootstrap_p_value": p_value,
        "insufficient": n < MIN_N_GRADE,
        "insufficient_data": grade == "INSUFFICIENT DATA",
    }


# ─── Moteur principal ────────────────────────────────────────────────────────

class BacktestEngine:

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _load_all(self) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM metrics_history ORDER BY ts ASC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _find_future_price(
        self, rows: List[Dict], ts_ref: int, horizon_s: int, tolerance_s: int = 7200
    ) -> Optional[float]:
        """Trouve le btc_price le plus proche de ts_ref + horizon_s (± tolerance_s)."""
        target = ts_ref + horizon_s
        best = None
        best_delta = float("inf")
        for r in rows:
            delta = abs(r["ts"] - target)
            if delta < best_delta and delta <= tolerance_s:
                best_delta = delta
                best = r["btc_price"]
        return best

    def enrich_with_outcomes(self, rows: List[Dict]) -> List[Dict]:
        """Ajoute perf_24h, perf_72h, perf_7j à chaque snapshot."""
        enriched = []
        for row in rows:
            r = dict(row)
            price_now = float(r.get("btc_price") or 0)
            if price_now <= 0:
                continue
            for key, horizon in HORIZONS.items():
                fp = self._find_future_price(rows, r["ts"], horizon)
                r[f"perf_{key}"] = round(_perf(price_now, fp), 3) if fp else None
            enriched.append(r)
        return enriched

    # ── Backtest par composante ───────────────────────────────────────────────

    def _backtest_condition(
        self, enriched: List[Dict], condition_fn, label: str, direction: str = "haussier"
    ) -> Dict:
        """direction: 'haussier' → gain si BTC monte, 'baissier' → gain si BTC baisse (short)."""
        matching = [r for r in enriched if condition_fn(r)]
        result = {"label": label, "direction": direction, "n_total": len(matching)}
        for h_key in HORIZONS:
            perfs = [r[f"perf_{h_key}"] for r in matching if r.get(f"perf_{h_key}") is not None]
            if direction == "baissier":
                # Inverser les perfs : -p > 0 si BTC baisse → gain pour un short
                stats = _compute_stats([-p for p in perfs])
            else:
                stats = _compute_stats(perfs)
            result[h_key] = stats
        return result

    def backtest_mopi(self, enriched: List[Dict]) -> Dict:
        return {
            "haussier": self._backtest_condition(
                enriched,
                lambda r: float(r.get("mopi") or 0) > 65,
                "MOPI > 65",
            ),
            "baissier": self._backtest_condition(
                enriched,
                lambda r: float(r.get("mopi") or 100) < 35,
                "MOPI < 35",
                direction="baissier",
            ),
            "neutre": self._backtest_condition(
                enriched,
                lambda r: 35 <= float(r.get("mopi") or 50) <= 65,
                "MOPI 35-65 (neutre)",
            ),
        }

    def backtest_gex(self, enriched: List[Dict]) -> Dict:
        GEX_NEUTRAL = 5_000_000
        return {
            "amplificateur": self._backtest_condition(
                enriched,
                lambda r: float(r.get("gex") or 0) < -GEX_NEUTRAL,
                "GEX AMPLIFICATEUR",
            ),
            "stabilisant": self._backtest_condition(
                enriched,
                lambda r: float(r.get("gex") or 0) > GEX_NEUTRAL,
                "GEX STABILISANT",
            ),
        }

    def backtest_dex(self, enriched: List[Dict]) -> Dict:
        return {
            "bullish": self._backtest_condition(
                enriched,
                lambda r: float(r.get("dex") or 0) < -500,
                "DEX BULLISH_FLOWS (delta < -500 BTC)",
            ),
            "bearish": self._backtest_condition(
                enriched,
                lambda r: float(r.get("dex") or 0) > 500,
                "DEX BEARISH_FLOWS (delta > 500 BTC)",
                direction="baissier",
            ),
        }

    def backtest_pcr(self, enriched: List[Dict]) -> Dict:
        def _pcr(r):
            v = r.get("pc_ratio_near") or r.get("pc_ratio")
            return float(v) if v is not None else 1.0

        return {
            "puts_dominants": self._backtest_condition(
                enriched,
                lambda r: _pcr(r) > 1.2,
                "PCR Near > 1.2 (puts dominants → contrarian bullish)",
            ),
            "calls_dominants": self._backtest_condition(
                enriched,
                lambda r: _pcr(r) < 0.8,
                "PCR Near < 0.8 (calls dominants → contrarian bearish)",
                direction="baissier",
            ),
        }

    def backtest_max_pain(self, enriched: List[Dict]) -> Dict:
        return {
            "aimant_haussier": self._backtest_condition(
                enriched,
                lambda r: float(r.get("max_pain") or 0) > float(r.get("btc_price") or 0) * 1.005,
                "Max Pain > BTC +0.5% (aimant haussier)",
            ),
            "aimant_baissier": self._backtest_condition(
                enriched,
                lambda r: float(r.get("max_pain") or 0) < float(r.get("btc_price") or 1) * 0.995,
                "Max Pain < BTC -0.5% (aimant baissier)",
                direction="baissier",
            ),
        }

    def backtest_squeeze_proxy(self, enriched: List[Dict]) -> Dict:
        """Squeeze proxy : GEX < 0 ET flip_level proche du prix (< 3% distance)."""
        def _squeeze_proxy(r):
            gex = float(r.get("gex") or 0)
            btc = float(r.get("btc_price") or 1)
            flip = float(r.get("flip_level") or 0)
            if gex >= 0 or flip <= 0:
                return False
            dist = abs(flip - btc) / btc
            return dist < 0.03

        return {
            "actif": self._backtest_condition(
                enriched,
                _squeeze_proxy,
                "Squeeze Proxy (GEX < 0 ET flip < 3% du prix)",
            ),
        }

    def backtest_gravity(self, enriched: List[Dict]) -> Dict:
        """Gravity Map proxy : max_pain par rapport au prix."""
        return {
            "au_dessus": self._backtest_condition(
                enriched,
                lambda r: float(r.get("max_pain") or 0) > float(r.get("btc_price") or 0),
                "Gravity au-dessus du prix (max_pain > spot)",
            ),
            "en_dessous": self._backtest_condition(
                enriched,
                lambda r: 0 < float(r.get("max_pain") or 0) < float(r.get("btc_price") or 1),
                "Gravity en dessous du prix (max_pain < spot)",
                direction="baissier",
            ),
        }

    def _build_signal_examples(
        self, enriched: List[Dict], signal_type: str, horizon: str = "72h", max_examples: int = 10
    ) -> List[Dict]:
        """V2 — Table d'exemples pour chaque signal backtest.

        Pour chaque signal déclenché, expose :
          - timestamp, direction, spot entrée, spot +72h, rendement,
            règles actives (GEX, DEX, Max Pain), raison du faux signal si perdant.
        """
        examples = []
        for r in enriched:
            if r.get("_v1_signal") != signal_type:
                continue
            ts = r.get("ts", 0)
            spot_entry = float(r.get("btc_price") or 0)
            spot_future = None
            perf = r.get(f"perf_{horizon}")

            if signal_type == "HAUSSIER":
                win = perf is not None and perf > 0
            else:
                win = perf is not None and perf < 0

            # Règles actives au moment du signal
            gex_val = float(r.get("gex") or 0)
            dex_val = float(r.get("dex") or 0)
            max_pain_val = float(r.get("max_pain") or 0)
            mopi_val = float(r.get("mopi") or 50)

            gex_regime = "AMPLIFICATEUR" if gex_val < -5_000_000 else ("STABILISANT" if gex_val > 5_000_000 else "NEUTRE")
            dex_direction = "BEARISH_FLOWS" if dex_val > 500 else ("BULLISH_FLOWS" if dex_val < -500 else "NEUTRAL")
            dex_score = _dex_to_score(dex_val)
            crash_gate = _is_crash_regime_v1(r, dex_score)

            raison_faux_signal = ""
            if not win and perf is not None:
                if crash_gate:
                    raison_faux_signal = f"Crash Regime actif (DEX {dex_score:.0f}% + GEX {gex_regime}) — Max Pain ignoré"
                elif gex_regime == "AMPLIFICATEUR" and dex_direction == "BEARISH_FLOWS":
                    raison_faux_signal = f"GEX AMPLIFICATEUR + DEX baissier — pression de hedging adverse"
                elif max_pain_val > spot_entry * 1.005:
                    raison_faux_signal = f"Max Pain ${max_pain_val:,.0f} au-dessus mais DEX/GEX contradictoires"
                else:
                    raison_faux_signal = "Signal BULL sans confluence DEX/GEX alignée"

            examples.append({
                "timestamp": ts,
                "timestamp_label": _ts_label(ts) if ts else "—",
                "direction": signal_type,
                "spot_entree": round(spot_entry, 0),
                f"spot_{horizon}": round(float(r.get(f"btc_price_{horizon}") or 0), 0) if r.get(f"btc_price_{horizon}") else None,
                "rendement_pct": round(perf, 2) if perf is not None else None,
                "gagnant": win,
                "regles_actives": {
                    "gex_regime": gex_regime,
                    "dex_direction": dex_direction,
                    "dex_score": round(dex_score, 1),
                    "max_pain": round(max_pain_val, 0),
                    "mopi": round(mopi_val, 1),
                    "crash_gate_actif": crash_gate,
                },
                "raison_faux_signal": raison_faux_signal if not win else "",
            })

        # Trier par rendement (perdants en premier pour visibilité)
        examples.sort(key=lambda x: (x.get("gagnant", True), x.get("rendement_pct") or 0))
        return examples[:max_examples]

    def backtest_signal_v1(self, enriched: List[Dict]) -> Dict:
        """Signal Mamos V1 — composite pondéré.

        V2 : inclut les champs de détail séparés :
          - n_snapshots_total : tous les snapshots collectés (NE PAS confondre avec N signaux)
          - n_signals_haussier : nb fois que le signal HAUSSIER a déclenché
          - n_signals_wins_72h / n_signals_losses_72h : wins et losses explicites
          - examples : table d'exemples pour audit des faux signaux
        """
        for r in enriched:
            r["_v1_signal"], r["_v1_score"] = compute_signal_mamos_v1(r)

        haussier_result = self._backtest_condition(
            enriched,
            lambda r: r.get("_v1_signal") == "HAUSSIER",
            "Signal V1 HAUSSIER (score > 60)",
        )
        baissier_result = self._backtest_condition(
            enriched,
            lambda r: r.get("_v1_signal") == "BAISSIER",
            "Signal V1 BAISSIER (score < 40)",
            direction="baissier",
        )
        neutre_result = self._backtest_condition(
            enriched,
            lambda r: r.get("_v1_signal") == "NEUTRE",
            "Signal V1 NEUTRE (score 40-60)",
        )

        # V2 — Champs N séparés : ne jamais confondre snapshots et signaux
        n_snapshots_total = len(enriched)
        n_signals_haussier = haussier_result.get("n_total", 0)
        n_signals_baissier = baissier_result.get("n_total", 0)

        # Stats wins/losses 72h pour HAUSSIER
        h72_stats = haussier_result.get("72h", {})
        n_tested_72h = h72_stats.get("n", 0) if h72_stats else 0
        wr_72h = h72_stats.get("winrate", 0) if h72_stats else 0
        n_wins_72h = round(n_tested_72h * wr_72h / 100) if n_tested_72h > 0 else 0
        n_losses_72h = n_tested_72h - n_wins_72h

        # Table d'exemples (10 pires signaux en priorité)
        examples_haussier = self._build_signal_examples(enriched, "HAUSSIER", "72h", max_examples=10)

        return {
            "haussier": haussier_result,
            "baissier": baissier_result,
            "neutre": neutre_result,
            # V2 — Séparation explicite N
            "stats_v2": {
                "n_snapshots_collectes": n_snapshots_total,
                "n_signaux_haussier_testes": n_signals_haussier,
                "n_signaux_baissier_testes": n_signals_baissier,
                "n_signaux_haussier_gagnants_72h": n_wins_72h,
                "n_signaux_haussier_perdants_72h": n_losses_72h,
                "note": (
                    "N snapshots collectés ≠ N signaux testés. "
                    "Le signal ne déclenche que quand le score V1 franchit les seuils (>60 ou <40). "
                    "Ne jamais présenter N_snapshots comme N_backtest."
                ),
            },
            "examples_haussier": examples_haussier,
        }

    # ── Matrice de confluence ─────────────────────────────────────────────────

    def backtest_confluence(self, enriched: List[Dict]) -> List[Dict]:
        """Phase 4 — Mesure l'EV des combinaisons d'indicateurs pour identifier les vrais moteurs."""
        for r in enriched:
            if "_v1_signal" not in r:
                r["_v1_signal"], r["_v1_score"] = compute_signal_mamos_v1(r)

        def _mopi_bull(r): return float(r.get("mopi") or 0) > 65
        def _dex_bull(r): return float(r.get("dex") or 0) < -500
        def _gex_amp(r): return float(r.get("gex") or 0) < -5_000_000
        def _gex_stab(r): return float(r.get("gex") or 0) > 5_000_000
        def _gravity_bull(r): return float(r.get("max_pain") or 0) > float(r.get("btc_price") or 0)
        def _pcr_bull(r):
            v = r.get("pc_ratio_near") or r.get("pc_ratio")
            return float(v) > 1.2 if v is not None else False

        combos = [
            ("MOPI > 65 (seul)",
             _mopi_bull),
            ("MOPI + DEX Bullish",
             lambda r: _mopi_bull(r) and _dex_bull(r)),
            ("MOPI + DEX + GEX Amplificateur",
             lambda r: _mopi_bull(r) and _dex_bull(r) and _gex_amp(r)),
            ("MOPI + DEX + GEX Stabilisant",
             lambda r: _mopi_bull(r) and _dex_bull(r) and _gex_stab(r)),
            ("MOPI + DEX + Gravity haussière",
             lambda r: _mopi_bull(r) and _dex_bull(r) and _gravity_bull(r)),
            ("MOPI + DEX + PCR contrarian",
             lambda r: _mopi_bull(r) and _dex_bull(r) and _pcr_bull(r)),
            ("MOPI + DEX + Gravity + PCR (4 signaux)",
             lambda r: _mopi_bull(r) and _dex_bull(r) and _gravity_bull(r) and _pcr_bull(r)),
            ("Signal V1 HAUSSIER (composite pondéré)",
             lambda r: r.get("_v1_signal") == "HAUSSIER"),
        ]

        return [self._backtest_condition(enriched, fn, label) for label, fn in combos]

    # ── Out-of-sample validation (70/30 chronologique) ────────────────────────

    def backtest_out_of_sample(self, enriched: List[Dict]) -> Dict:
        """Split 70/30 chronologique → valide que le signal n'est pas overfit.
        In-sample = 70% les plus anciens. Out-of-sample = 30% les plus récents.
        Si EV out-of-sample >>> EV in-sample → possible overfitting.
        """
        if len(enriched) < MIN_N_VALID * 2:
            return {"status": "INSUFFICIENT_DATA", "n_total": len(enriched)}

        split = int(len(enriched) * 0.70)
        in_sample = enriched[:split]
        out_sample = enriched[split:]

        for r in in_sample + out_sample:
            if "_v1_signal" not in r:
                r["_v1_signal"], r["_v1_score"] = compute_signal_mamos_v1(r)

        def _v1_bull(r):
            return r.get("_v1_signal") == "HAUSSIER"

        oos_in = self._backtest_condition(in_sample, _v1_bull, "Signal V1 HAUSSIER — In-Sample (70%)")
        oos_out = self._backtest_condition(out_sample, _v1_bull, "Signal V1 HAUSSIER — Out-of-Sample (30%)")

        # Déterminer la dégradation out-of-sample
        def _ev_at(cond_stats: Dict, horizon: str) -> Optional[float]:
            h = cond_stats.get(horizon, {})
            if h and "ev" in h and not h.get("insufficient"):
                return h["ev"]
            return None

        degradation = {}
        for h in ("24h", "72h"):
            ev_in = _ev_at(oos_in, h)
            ev_out = _ev_at(oos_out, h)
            if ev_in is not None and ev_out is not None:
                diff = ev_out - ev_in
                degradation[h] = {
                    "ev_in_sample": ev_in,
                    "ev_out_of_sample": ev_out,
                    "degradation": round(diff, 2),
                    "stable": abs(diff) < 2.0,
                }

        return {
            "status": "OK",
            "n_in_sample": len(in_sample),
            "n_out_of_sample": len(out_sample),
            "in_sample": oos_in,
            "out_of_sample": oos_out,
            "degradation": degradation,
            # all([]) == True in Python — None when no OOS data is available
            "oos_stable": all(v.get("stable", False) for v in degradation.values()) if degradation else None,
        }

    # ── Stabilité par régime GEX ──────────────────────────────────────────────

    def backtest_stability_by_regime(self, enriched: List[Dict]) -> Dict:
        """Signal V1 splité par régime GEX (STABILISANT vs AMPLIFICATEUR).
        Permet de savoir si le signal fonctionne différemment selon le régime.
        """
        GEX_NEUTRAL = 5_000_000

        def _stab(r):
            return float(r.get("gex") or 0) > GEX_NEUTRAL

        def _amp(r):
            return float(r.get("gex") or 0) < -GEX_NEUTRAL

        for r in enriched:
            if "_v1_signal" not in r:
                r["_v1_signal"], r["_v1_score"] = compute_signal_mamos_v1(r)

        stabilisant = [r for r in enriched if _stab(r)]
        amplificateur = [r for r in enriched if _amp(r)]

        def _cond_bull(subset):
            return self._backtest_condition(
                subset,
                lambda r: r.get("_v1_signal") == "HAUSSIER",
                "Signal V1 HAUSSIER",
            )

        return {
            "STABILISANT": _cond_bull(stabilisant) if len(stabilisant) >= MIN_N_VALID else {
                "n_total": len(stabilisant), "status": "INSUFFICIENT_DATA"
            },
            "AMPLIFICATEUR": _cond_bull(amplificateur) if len(amplificateur) >= MIN_N_VALID else {
                "n_total": len(amplificateur), "status": "INSUFFICIENT_DATA"
            },
            "n_stabilisant": len(stabilisant),
            "n_amplificateur": len(amplificateur),
        }

    # ── Argument VIP ──────────────────────────────────────────────────────────

    def generate_vip_argument(self, full_result: Dict) -> str:
        """Phase 6 — Argumentaire VIP basé sur EV réelle, jamais sur winrate seul."""
        lines = ["━" * 48, "ARGUMENT VIP — EXPECTED VALUE", "━" * 48]

        if full_result.get("status") != "OK":
            lines.append("Données en cours d'accumulation — disponible sous ~30 jours.")
            return "\n".join(lines)

        meta = full_result.get("meta", {})
        span = meta.get("span_days", 0)

        # Signal V1 HAUSSIER à 72h — le plus exploitable
        sv1_h = full_result.get("signal_v1", {}).get("haussier", {})
        h72 = sv1_h.get("72h", {})
        n_sigs = (h72 or {}).get("n", 0)
        if h72 and "ev" in h72:
            ev = h72["ev"]
            grade = h72["grade"]
            gain = h72["gain_moyen"]
            perte = h72["perte_moyenne"]
            wr = h72["winrate"]
            conf_label = h72.get("confidence_label", "Low")
            conf_score = h72.get("confidence_score", 0)
            lines.append(f"\nSignal HAUSSIER — {n_sigs} occurrences sur {span:.0f} jours :")
            lines.append(f"  Winrate     : {wr:.0f}%")
            lines.append(f"  Gain moyen  : +{gain:.2f}%")
            lines.append(f"  Perte moy   : -{perte:.2f}%")
            lines.append(f"  EV à 72h    : {ev:+.2f}%")
            lines.append(f"  Confiance   : {conf_label} ({conf_score}/100)")
            if n_sigs < MIN_N_GRADE:
                lines.append(f"  Grade       : En phase de validation (N={n_sigs} < {MIN_N_GRADE} requis)")
                lines.append("")
                lines.append("⚠️ ARGUMENT VIP BLOQUÉ")
                lines.append(f'  Données insuffisantes — N minimum requis : {MIN_N_GRADE}')
                lines.append(f'  Accumulation en cours ({n_sigs}/{MIN_N_GRADE}).')
            else:
                lines.append(f"  Grade       : {grade}")
                lines.append("")
                if grade in ("A", "B"):
                    lines.append('💬 Message VIP :')
                    lines.append(f'  "Nos alertes {grade} ont généré en moyenne +{gain:.1f}%')
                    lines.append(f'   à 72h sur les {span:.0f} derniers jours ({n_sigs} signaux).')
                    lines.append(f'   Confiance statistique : {conf_label}."')
                elif grade == "C":
                    lines.append('💬 Message VIP :')
                    lines.append(f'  "Signal rentable sur {wr:.0f}% des cas — EV positive confirmée."')
                else:
                    lines.append("⚠️ Signal pas encore validé pour argument VIP public.")
        else:
            lines.append("Données 72h insuffisantes — accumulation ~30 jours nécessaire.")

        # Ranking EV +24h par composante
        components = full_result.get("components", {})
        ev_rows: List[Tuple] = []
        for comp, comp_data in components.items():
            for direction, cond_stats in comp_data.items():
                h = cond_stats.get("24h", {})
                if h and "ev" in h:
                    label = cond_stats.get("label", f"{comp} {direction}")
                    ev_rows.append((
                        label, h["ev"], h["grade"],
                        h.get("confidence_label", "Low"), h.get("n", 0),
                    ))
        ev_rows.sort(key=lambda x: x[1], reverse=True)

        if ev_rows:
            lines.append("\n━ RANKING EV +24h (composantes) ━━━━━━━━━━━━━━━")
            for i, (lbl, ev_val, grd, conf_lbl, n_c) in enumerate(ev_rows[:10], 1):
                if grd == "—":
                    icon = "🔬"
                    grd_display = f"VALIDATION (N={n_c})"
                else:
                    icon = "🔥" if grd == "A" else ("✅" if grd == "B" else ("➡️" if grd == "C" else "⚠️"))
                    grd_display = f"{grd} | {conf_lbl}"
                lines.append(f"  {i:>2}. [{grd_display}] {ev_val:+.2f}%  {lbl} {icon}")

        # Confluence : quelle combinaison gagne le plus
        confluence = full_result.get("confluence", [])
        ev_conf: List[Tuple] = []
        for cond_stats in confluence:
            h = cond_stats.get("72h", {})
            if h and "ev" in h:
                ev_conf.append((
                    cond_stats["label"], h["ev"], h["grade"],
                    h["n"], h.get("confidence_label", "Low"),
                ))
        ev_conf.sort(key=lambda x: x[1], reverse=True)

        if ev_conf:
            lines.append("\n━ MATRICE DE CONFLUENCE — EV +72h ━━━━━━━━━━━━━")
            for lbl, ev_val, grd, n_c, conf_lbl in ev_conf:
                if grd == "—":
                    icon = "🔬"
                    tag = f"VALIDATION N={n_c}"
                else:
                    icon = "🔥" if grd == "A" else ("✅" if grd == "B" else ("➡️" if grd == "C" else "⚠️"))
                    tag = f"{grd} | {conf_lbl}"
                lines.append(f"  [{tag}] {ev_val:+.2f}% (N={n_c:>3})  {lbl} {icon}")

        lines.append("\n" + "━" * 48)
        lines.append("RÈGLE : Un indicateur n'est utile que s'il améliore l'EV.")
        lines.append("Tout le reste est du bruit visuel.")
        lines.append("━" * 48)
        return "\n".join(lines)

    # ── Recommandations de poids ──────────────────────────────────────────────

    def _predictive_power(self, stats: Dict, horizon: str = "24h") -> float:
        """Score de pouvoir prédictif sur un horizon (winrate ajusté par N et intensité)."""
        h = stats.get(horizon, {})
        if not h or h.get("insufficient") or h.get("n", 0) < MIN_N_VALID:
            return 0.0
        winrate = h.get("winrate", 50)
        n = h.get("n", 0)
        # Écart par rapport au hasard, pondéré par N (plus de confiance avec plus de données)
        signal_strength = abs(winrate - 50)
        confidence = min(1.0, n / 50)  # pleine confiance à 50 points
        return signal_strength * confidence

    def recommend_weights(self, results: Dict) -> Dict:
        """Compare le pouvoir prédictif de chaque composante et suggère des ajustements."""
        components = {
            "MOPI":    max(
                self._predictive_power(results["mopi"]["haussier"]),
                self._predictive_power(results["mopi"]["baissier"]),
            ),
            "GEX":     max(
                self._predictive_power(results["gex"]["amplificateur"]),
                self._predictive_power(results["gex"]["stabilisant"]),
            ),
            "DEX":     max(
                self._predictive_power(results["dex"]["bullish"]),
                self._predictive_power(results["dex"]["bearish"]),
            ),
            "PCR":     max(
                self._predictive_power(results["pcr"]["puts_dominants"]),
                self._predictive_power(results["pcr"]["calls_dominants"]),
            ),
            "MaxPain": max(
                self._predictive_power(results["max_pain"]["aimant_haussier"]),
                self._predictive_power(results["max_pain"]["aimant_baissier"]),
            ),
            "Squeeze": self._predictive_power(results["squeeze"]["actif"]),
            "Gravity": max(
                self._predictive_power(results["gravity"]["au_dessus"]),
                self._predictive_power(results["gravity"]["en_dessous"]),
            ),
        }

        total_power = sum(components.values()) or 1.0
        current_weights = {
            "GEX": 0.40, "IV": 0.25, "PCR": 0.20, "Squeeze": 0.15
        }

        sorted_components = sorted(components.items(), key=lambda x: x[1], reverse=True)
        top = sorted_components[0][0] if sorted_components else "MOPI"
        bottom = sorted_components[-1][0] if len(sorted_components) > 1 else None

        rationale_parts = []
        if components[top] > 10:
            rationale_parts.append(
                f"{top} montre la plus forte valeur prédictive (écart {components[top]:.1f}% vs 50%)"
            )
        if bottom and components[bottom] < 2 and components[bottom] < components[top] / 3:
            rationale_parts.append(
                f"{bottom} apporte peu de signal prédictif — candidat à réduction de poids"
            )
        if not rationale_parts:
            rationale_parts.append("Données insuffisantes pour recommander des ajustements")

        return {
            "predictive_power": {k: round(v, 1) for k, v in components.items()},
            "current_weights": current_weights,
            "ranking": [k for k, _ in sorted_components],
            "rationale": ". ".join(rationale_parts),
        }

    # ── Rapport complet ───────────────────────────────────────────────────────

    def full_report(self, days: int = 90) -> Dict:
        """Rapport backtest complet. Retourne un dict structuré + un résumé texte."""
        all_rows = self._load_all()

        # Filtrer sur la fenêtre demandée
        cutoff = int(time.time()) - days * 86400
        rows = [r for r in all_rows if r["ts"] >= cutoff]

        n = len(rows)
        if n == 0:
            return {
                "status": "NO_DATA",
                "message": "Aucune donnée dans la base. Le système commence à enregistrer.",
                "n": 0,
            }

        ts_min = rows[0]["ts"]
        ts_max = rows[-1]["ts"]
        span_days = (ts_max - ts_min) / 86400

        # Enrichir avec les outcomes futurs
        enriched = self.enrich_with_outcomes(rows)

        # Vérifier si on a des outcomes valides
        has_24h = any(r.get("perf_24h") is not None for r in enriched)
        has_72h = any(r.get("perf_72h") is not None for r in enriched)
        has_7j  = any(r.get("perf_7j") is not None for r in enriched)

        if not has_24h:
            return {
                "status": "ACCUMULATING",
                "message": (
                    f"N={n} snapshots sur {span_days:.1f} jours. "
                    f"Les outcomes +24h seront disponibles dès que des données "
                    f"enregistrées il y a >24h existent. "
                    f"ETA pour backtest complet : ~{max(0, 7 - span_days):.1f} jours."
                ),
                "n": n,
                "span_days": round(span_days, 1),
                "horizons_ready": {
                    "24h": has_24h,
                    "72h": has_72h,
                    "7j": has_7j,
                },
                "snapshot": {
                    "from": _ts_label(ts_min),
                    "to": _ts_label(ts_max),
                },
            }

        # Lancer tous les backtests
        results = {
            "mopi":      self.backtest_mopi(enriched),
            "gex":       self.backtest_gex(enriched),
            "dex":       self.backtest_dex(enriched),
            "pcr":       self.backtest_pcr(enriched),
            "max_pain":  self.backtest_max_pain(enriched),
            "squeeze":   self.backtest_squeeze_proxy(enriched),
            "gravity":   self.backtest_gravity(enriched),
        }
        signal_v1 = self.backtest_signal_v1(enriched)
        confluence = self.backtest_confluence(enriched)
        weights_rec = self.recommend_weights(results)
        out_of_sample = self.backtest_out_of_sample(enriched)
        stability_by_regime = self.backtest_stability_by_regime(enriched)

        full_result = {
            "status": "OK",
            "meta": {
                "n_snapshots": n,
                "span_days": round(span_days, 1),
                "from": _ts_label(ts_min),
                "to": _ts_label(ts_max),
                "horizons_ready": {
                    "4h":  any(r.get("perf_4h")  is not None for r in enriched),
                    "24h": has_24h,
                    "72h": has_72h,
                    "7j":  has_7j,
                },
                "warning": (
                    "DONNÉES INSUFFISANTES — statistiques peu fiables (N < 50)"
                    if n < 50 else None
                ),
                "min_n_required": MIN_N_GRADE,
                "insufficient_data_threshold": MIN_N_GRADE,
            },
            "components": results,
            "signal_v1": signal_v1,
            "confluence": confluence,
            "weights": weights_rec,
            "out_of_sample": out_of_sample,
            "stability_by_regime": stability_by_regime,
        }
        full_result["text_report"] = self.generate_text_report(
            results, signal_v1, weights_rec, confluence, n, span_days
        )
        full_result["vip_argument"] = self.generate_vip_argument(full_result)
        return full_result

    def generate_text_report(
        self,
        results: Dict,
        signal_v1: Dict,
        weights: Dict,
        confluence: List[Dict],
        n: int,
        span_days: float,
    ) -> str:
        lines = []
        lines.append("═" * 50)
        lines.append("BACKTEST SIGNAL MAMOS — RAPPORT COMPLET")
        lines.append("═" * 50)
        lines.append(f"Données : {n} snapshots sur {span_days:.1f} jours")
        if n < MIN_N_STATS:
            lines.append(f"⚠️  ATTENTION : N={n} — statistiques peu fiables avant N≥{MIN_N_STATS}")
        lines.append("")

        # Composantes individuelles
        lines.append("COMPOSANTES INDIVIDUELLES")
        lines.append("─" * 40)

        component_groups = [
            ("MOPI", [
                ("MOPI > 65 (pression haussière)", results["mopi"]["haussier"]),
                ("MOPI < 35 (pression baissière)", results["mopi"]["baissier"]),
            ]),
            ("GEX", [
                ("GEX AMPLIFICATEUR", results["gex"]["amplificateur"]),
                ("GEX STABILISANT",   results["gex"]["stabilisant"]),
            ]),
            ("DEX", [
                ("DEX Flows Haussiers",  results["dex"]["bullish"]),
                ("DEX Flows Baissiers",  results["dex"]["bearish"]),
            ]),
            ("PCR Near", [
                ("PCR > 1.2 (puts dominants, contrarian haussier)", results["pcr"]["puts_dominants"]),
                ("PCR < 0.8 (calls dominants, contrarian baissier)", results["pcr"]["calls_dominants"]),
            ]),
            ("Max Pain", [
                ("Max Pain au-dessus du prix", results["max_pain"]["aimant_haussier"]),
                ("Max Pain en dessous du prix", results["max_pain"]["aimant_baissier"]),
            ]),
            ("Squeeze Proxy", [
                ("Squeeze actif (GEX<0 ET flip<3%)", results["squeeze"]["actif"]),
            ]),
            ("Gravity Map", [
                ("Gravity au-dessus", results["gravity"]["au_dessus"]),
                ("Gravity en dessous", results["gravity"]["en_dessous"]),
            ]),
        ]

        for group_name, conditions in component_groups:
            lines.append(f"\n{group_name}:")
            for label, stats_dict in conditions:
                lines.append(f"  {label}")
                for h_key in ["24h", "72h", "7j"]:
                    h = stats_dict.get(h_key, {})
                    if not h:
                        lines.append(f"    +{h_key:<4}: —")
                        continue
                    if h.get("insufficient"):
                        lines.append(f"    +{h_key:<4}: N={h.get('n',0)} (insuffisant)")
                        continue
                    if "ev" in h:
                        grd = h.get('grade', '?')
                        conf = h.get('confidence_label', 'Low')
                        conf_s = h.get('confidence_score', 0)
                        grade_tag = f"[{grd}|{conf} {conf_s}]" if grd not in ("—", "INSUFFICIENT DATA") else f"[INSUFFICIENT DATA N={h['n']}]"
                        ev_str = f"EV {h['ev']:>+5.2f}% {grade_tag}"
                    else:
                        ev_str = ""
                    lines.append(
                        f"    +{h_key:<4}: "
                        f"N={h['n']:>4} | "
                        f"Winrate {h['winrate']:>5.1f}% | "
                        f"{ev_str:<30} | "
                        f"Gain {h.get('gain_moyen', 0):>+5.2f}% / Perte -{h.get('perte_moyenne', 0):>4.2f}% | "
                        f"Max DD {h['max_dd']:>+6.2f}%"
                    )

        # Signal Mamos V1
        lines.append("\n" + "─" * 40)
        lines.append("SIGNAL MAMOS V1 (composite)")
        lines.append(f"  Poids : MOPI {WEIGHTS_V1['mopi']*100:.0f}% | "
                     f"DEX {WEIGHTS_V1['dex']*100:.0f}% | "
                     f"Gravity {WEIGHTS_V1['gravity']*100:.0f}% | "
                     f"Flip {WEIGHTS_V1['flip']*100:.0f}% | "
                     f"PCR {WEIGHTS_V1['pcr']*100:.0f}%")
        for sig_key, sig_label in [("haussier", "HAUSSIER (>60)"), ("baissier", "BAISSIER (<40)"), ("neutre", "NEUTRE (40-60)")]:
            lines.append(f"\n  Signal {sig_label}")
            stats_dict = signal_v1.get(sig_key, {})
            for h_key in ["24h", "72h", "7j"]:
                h = stats_dict.get(h_key, {})
                if not h:
                    lines.append(f"    +{h_key:<4}: —")
                    continue
                if h.get("insufficient"):
                    lines.append(f"    +{h_key:<4}: N={h.get('n',0)} (insuffisant)")
                    continue
                lines.append(
                    f"    +{h_key:<4}: "
                    f"N={h['n']:>4} | "
                    f"Winrate {h['winrate']:>5.1f}% | "
                    f"Perf moy {h['perf_moy']:>+6.2f}% | "
                    f"Max DD {h['max_dd']:>+6.2f}%"
                )

        # Matrice de confluence
        lines.append("\n" + "─" * 40)
        lines.append("MATRICE DE CONFLUENCE — EV +72h")
        lines.append("(Que gagne un trader qui suit exactement ce signal ?)")
        if confluence:
            for cond_stats in confluence:
                lbl = cond_stats.get("label", "?")
                h = cond_stats.get("72h", {})
                if not h or h.get("insufficient"):
                    n_c = (h or {}).get("n", 0)
                    lines.append(f"  {lbl:<45} N={n_c} (insuffisant)")
                    continue
                ev_v = h.get("ev", 0)
                grd = h.get("grade", "?")
                wr = h.get("winrate", 0)
                n_c = h.get("n", 0)
                conf_lbl = h.get("confidence_label", "Low")
                conf_s = h.get("confidence_score", 0)
                if grd == "—":
                    icon = "🔬"
                    tag = f"VALIDATION N={n_c}"
                else:
                    icon = "🔥" if grd == "A" else ("✅" if grd == "B" else ("➡️" if grd == "C" else "⚠️"))
                    tag = f"{grd}|{conf_lbl} {conf_s}"
                lines.append(
                    f"  [{tag}] {icon}  EV {ev_v:>+5.2f}%  "
                    f"Winrate {wr:>5.1f}%  N={n_c:>3}  {lbl}"
                )
        else:
            lines.append("  Pas encore de données de confluence.")

        # Recommandations
        lines.append("\n" + "─" * 40)
        lines.append("POUVOIR PRÉDICTIF (écart vs 50% winrate)")
        ranking = weights.get("ranking", [])
        powers = weights.get("predictive_power", {})
        for i, comp in enumerate(ranking, 1):
            power = powers.get(comp, 0)
            bar_len = int(power / 2)
            bar = "█" * bar_len + "░" * (10 - min(10, bar_len))
            lines.append(f"  {i}. {comp:<10} {bar} {power:.1f}%")

        lines.append("\n" + "─" * 40)
        lines.append("RECOMMANDATIONS")
        lines.append(weights.get("rationale", "Données insuffisantes."))

        lines.append("\n" + "═" * 50)
        return "\n".join(lines)


def _ts_label(ts: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


def run_backtest(days: int = 90) -> Dict:
    """Point d'entrée principal — utilisé par l'endpoint FastAPI."""
    engine = BacktestEngine()
    return engine.full_report(days=days)
