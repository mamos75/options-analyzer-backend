# API_CONTRACT.md — Contrat d'API Options Dashboard

> **Règle :** tout nouveau champ ou changement de payload est documenté ici dans le même commit.
> Les champs `DEPRECATED` ont un alias maintenu pendant une release avant suppression.

---

## Endpoints

### GET /api/dashboard
Payload principal du dashboard. Response model : `DashboardResponse`.

**Champs notables :**
| Champ | Type | Note |
|---|---|---|
| `weather_color` | `str` | Hex couleur du régime météo. **Nouveau B6.** Le frontend utilise ce champ directement — plus de map locale. |
| `mopi_squeeze_heuristic` | `float` | Composant interne MOPI (0-100). Nom définitif depuis B3. |
| `squeeze_prob` | `float` | **DEPRECATED** (alias compat). Sera supprimé dans la prochaine release majeure. |

---

### GET /api/options_walls
Walls d'options triées par `total_oi` décroissant (tri B2).

**Champs :**
| Champ | Type | Note |
|---|---|---|
| `major_call_wall` | `float \| null` | Strike max call OI au-dessus du spot. **`null` si aucun call wall** (B2 — anciennement spot). |
| `major_put_wall` | `float \| null` | Strike max put OI en-dessous du spot. **`null` si aucun put wall** (B2). |
| `walls` | `list` | Triées par `total_oi` décroissant. Champ `notional_usd` disponible. |

---

### GET /api/model_arena/bme_status
Statut et backtest du BTC Momentum Engine.

**Backtest — champs B1 (OOS strict) :**
| Champ | Type | Note |
|---|---|---|
| `eval_is_oos` | `bool` | Toujours `true` depuis B1 — backtest OOS avec embargo. |
| `n_out_of_sample` | `int` | Samples évalués (après split 80% + embargo). |
| `n_overlap_excluded` | `int` | Samples exclus par embargo (fenêtre outcome chevauche train). |
| `has_edge` | `bool` | Calcul serveur : `n>=30 AND wilson_lower(wr, n) > 0.50`. **Remplace le seuil local 0.52 frontend.** |
| `wilson_lb` | `float \| null` | Borne inférieure Wilson 95% IC (observabilité). |
| `contrarian_mode` | `bool` | Actif si `wilson_upper(wr, n) < 0.50` (test Wilson, pas seuil naïf 0.40). |
| `contrarian_decided_at` | `int` | Timestamp UNIX d'activation du contrarian (0 si inactif). |
| `dir_winrate` | `float \| null` | WR directionnel OOS. `null` si `reason: "contrarian_insufficient_oos"`. |

**Contrarian — règle B1 :**
- Ancien : `dir_wr < 0.40 AND n >= 30` → **incorrect** (ignore l'incertitude statistique)
- Nouveau : `wilson_upper(wr, n, z=1.96) < 0.50` → significativement pire que le hasard
- Exemple : WR=40%/n=30 → `wilson_upper ≈ 0.58 > 0.50` → **NON activé** (ancienne règle l'activait à tort)
- Exemple : WR=30%/n=100 → `wilson_upper ≈ 0.39 < 0.50` → **ACTIVÉ** (significatif)

---

### GET /api/snapshot *(nouveau B5)*
Endpoint agrégé — retourne tous les payloads depuis un seul snapshot Deribit.

**Structure racine :**
```json
{
  "snapshot_ts": 1700000000.0,
  "spot": 50000.0,
  "dashboard": { ... },
  "walls": { ... },
  "dealer": { ... },
  "squeeze": { ... },
  "narrative": { ... },
  "gravity": { ... },
  "bme_status": { ... }
}
```

**Garanties :** `snapshot_ts` identique dans tous les sous-payloads — cohérence totale.
Les 8 endpoints individuels restent disponibles (compatibilité frontend embarqué).

---

## Conventions dealer (B6)

Le dashboard utilise **deux hypothèses** différentes selon le module :

### GEX — hypothèse mixte (clients longs options)
```
GEX_call = +gamma × OI × spot²    (stabilisant — hedging pro-tendance)
GEX_put  = -gamma × OI × spot²    (déstabilisant — contra-tendance)
```
**Hypothèse :** les clients sont longs sur calls ET sur puts.
Le dealer est donc short calls et short puts.

### DEX — hypothèse short-all (dealers short toutes options)
```
DEX = -delta_instrument × OI × spot
```
**Hypothèse :** dealer short toutes les options (calls et puts).

**Pourquoi deux hypothèses ?** Les deux approches capturent des aspects complémentaires du flow.
Décision de les unifier ou non : à valider avec les données réelles (B6 — pending).

---

## Champs dépréciés

| Champ | Endpoint | Remplacé par | Suppression prévue |
|---|---|---|---|
| `squeeze_prob` | `/api/dashboard`, `/api/snapshot` | `mopi_squeeze_heuristic` | Release suivante |

---

## Tri des walls

Les `walls` retournés par `/api/options_walls` et `/api/snapshot` sont **triés par `total_oi` décroissant**.
Le frontend NE DOIT PAS re-trier — se fier à l'ordre serveur.

---

*Dernière mise à jour : B1-B6 (juillet 2026)*

---

## Frontend — Contrat de consommation

### Endpoints consommés

| Endpoint | Module widget | Champs requis |
|---|---|---|
| `GET /api/market_decision` | `widgets/signal.js` | `directional.direction`, `directional.confidence`, `watch_message`, `warnings[]` |
| `GET /api/options_walls` | `widgets/levels.js` | `walls[]`, `btc_price`, `major_call_wall`, `major_put_wall` |
| `GET /api/probability_engine` | `widgets/probabilities.js` | `bull_24h.probability`, `bear_24h.probability`, `bull_72h.probability`, `bear_72h.probability` |
| `GET /api/dashboard` | `widgets/context.js` | `weather_state`, `mopi_score`, `gex_regime`, `weather_color` |
| `GET /api/dealer_pressure` | `widgets/context.js` | `direction`, `mopi_score` |
| `GET /api/narrative` | `widgets/narrative.js` | `phrase_synthese`, `banner_message?`, `niveau_haut?`, `niveau_bas?` |
| `GET /api/model_arena/leaderboard` | `widgets/model.js` | `[0].model_name`, `[0].win_rate`, `[0].total_predictions` |
| `GET /api/vol_structure` | `widgets/vol_weather.js` | `data[].expiry`, `data[].iv`, `data[].oi_pct` |
| `GET /api/mopi_vs_btc` | `widgets/gex_dex.js`, `widgets/mopi_btc.js` | `gex[]`, `dex[]`, `mopi[]`, `btc_price[]`, `timestamps[]`, `correlations` |
| `GET /api/vex_cex` | `widgets/vex_cex.js`, `widgets/regime.js` | `vex_total`, `cex_total`, `vex_total_fmt`, `cex_total_fmt`, `vex_direction`, `cex_direction`, `vex_interpretation`, `cex_interpretation`, `vex_by_strike[]`, `cex_by_strike[]`, `gamma_flip`, `gamma_flip_dist_pct`, `gamma_flip_side`, `gamma_flip_regime`, `gamma_flip_interpretation` |
| `GET /api/vex_cex_history` | `widgets/vex_cex.js` | `points[].ts`, `points[].vex`, `points[].cex` |
| `GET /api/snapshot` | scheduler (`loadAllData`) | `snapshot_ts`, `spot`, `dashboard`, `walls`, `dealer`, `squeeze`, `narrative`, `bme_status` |

### Règles frontend

1. **XSS** : tout champ API de type `string` injecté dans `innerHTML` passe par `esc()` (`js/lib/fmt.js`).
2. **Résilience** : `Promise.allSettled` — une panne d'endpoint n'empêche pas les autres widgets de rendre.
3. **Race condition** : `AbortController` + sequence token — chaque cycle `loadAllData()` annule le précédent.
4. **Données périmées** : un module en erreur reçoit `data-stale="1"` et affiche un badge amber "Données périmées".
5. **Walls** : le frontend **ne re-trie pas** les `walls[]` — se fie à l'ordre serveur (`total_oi` décroissant).
6. **`squeeze_prob`** : champ déprécié — ne pas utiliser. Lire `mopi_squeeze_heuristic`.
7. **`has_edge`** : calculé côté serveur (`wilson_lower(wr,n) > 0.50`, n≥30). La fonction `clientHasEdge()` (`js/lib/stats.js`) sert à la cross-validation uniquement.
8. **Précision prix** : `fmtPrice()` adapte les décimales automatiquement (0 dp si ≥$10k, 1 dp si ≥$1k, 2 dp sinon).

### Architecture ES modules (Phase 5)

```
js/
  main.js          → entry point, expose window.* pour onclick HTML
  config.js        → CFG, API_BASE, REFRESH_INTERVAL
  store.js         → état partagé (pas de window.* globaux)
  api.js           → apiFetch(endpoint, signal)
  auth.js          → checkAuth, Patreon OAuth, JWT legacy
  scheduler.js     → loadAllData, startRefreshLoop, visibilitychange
  lib/
    stats.js       → wilsonLB(), clientHasEdge()
    fmt.js         → esc(), fmtPrice(), fmtBig(), tagBadge()
    canvas.js      → drawSparkline(), drawDualAxis(), drawVcLine()
  widgets/
    signal.js      probabilities.js  levels.js  context.js
    narrative.js   model.js          vol_weather.js
    gex_dex.js     mopi_btc.js       regime.js  vex_cex.js
```

*Dernière mise à jour : Phase 6 (juillet 2026)*
