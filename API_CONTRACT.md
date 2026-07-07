# API Contract — /api/decision

**Version F8.3** — Vocabulaire desk premium
**Date dépreciation aliases** : 21/07/2026

## Champs state + action (nouveaux — F8.3)

### `state` — Situation actuelle

| Valeur | Description |
|--------|-------------|
| `RAS` | Pas de signal / données insuffisantes |
| `TENSION` | Signal présent, pas de contradiction majeure |
| `CONFLIT` | Contradictions détectées entre indicateurs |
| `ZONE_CRITIQUE` | Signal directionnel mais confiance < 40% |

### `action` — Ce qu'il faut faire

| Valeur | Description |
|--------|-------------|
| `OBSERVER` | Pas d'action, surveiller |
| `PRÉPARER` | Signal directionnel mais confiance insuffisante pour agir |
| `AGIR_LONG` | Signal haussier convergent, confiance >= 60% |
| `AGIR_SHORT` | Signal baissier convergent, confiance >= 60% |

## Aliases mécaniques (dépreciés le 21/07/2026)

- `verdict` → alias de `action` (mapping : SIGNAL_UP→AGIR_LONG, OBSERVE→OBSERVER, etc.)
- `system_status` → alias de `state` (mapping : TRADEABLE→TENSION, CONFLICT→CONFLIT, etc.)

## Mapping `verdict` → `action`

| verdict | action (confiance < 60%) | action (confiance >= 60%) |
|---------|--------------------------|---------------------------|
| `SIGNAL_UP` | `PRÉPARER` | `AGIR_LONG` |
| `SIGNAL_DOWN` | `PRÉPARER` | `AGIR_SHORT` |
| `OBSERVE` | `OBSERVER` | `OBSERVER` |
| `NO_TRADE` | `OBSERVER` | `OBSERVER` |

## Mapping `system_status` → `state`

| system_status | state |
|---------------|-------|
| `OFFLINE` | `RAS` |
| `DEGRADED` | `RAS` |
| `CONFLICT` | `CONFLIT` |
| `TRADEABLE` | `TENSION` |
| `OBSERVE` | `TENSION` ou `ZONE_CRITIQUE` (selon confiance) |

---

## CHANGELOG — F15 Retrait MOPI (07/07/2026)

### Endpoints supprimés

| Endpoint | Supprimé le | Remplacé par |
|----------|-------------|--------------|
| `/api/mopi_free` | 07/07/2026 | n/a |
| `/api/mopi_validation` | 07/07/2026 | n/a |
| `/api/mopi_vs_btc` | 07/07/2026 | `/api/gex_dex_history` |
| `/api/mopi_divergence` | 07/07/2026 | n/a |

### Nouveau endpoint

`GET /api/gex_dex_history?period=7d&resolution=1h`

Remplace `/api/mopi_vs_btc` comme source du widget GEX & DEX Evolution.

| Champ | Type | Description |
|-------|------|-------------|
| `status` | string | OK ou ERROR |
| `period` | string | 7d, 14d, 30d |
| `resolution` | string | 30m, 1h, 4h, 1d |
| `n_points` | int | Nombre de points retournes |
| `timestamps` | int[] | Timestamps Unix (secondes) |
| `gex` | float[] | GEX total en USD |
| `dex` | float[] | DEX net delta en BTC |
| `btc_price` | float[] | Prix BTC spot |

### Champs supprimes de `/api/dashboard` (07/07/2026)

- `mopi_score` (float 0-100)
- `mopi_label` (string)
- `mopi_emoji` (string)
- `mopi_gex_component` (float)
- `mopi_pc_component` (float)
- `mopi_squeeze_heuristic` (float)
- `squeeze_prob` (float, DEPRECATED alias)
- `mopi_delta_24h` (float|null)

Note Option A: `compute_mopi()` continue en arriere-plan (collecte silencieuse DB).
La colonne `mopi` reste dans `metrics_history` pour usage analytique futur.

### Renormalisation directional_bias (F15.4)

Poids des signaux apres suppression de MOPI (ancien poids: 30%):

| Signal | Ancien poids | Nouveau poids |
|--------|-------------|---------------|
| DEX | 40% | 50% |
| PCR | 20% | 28% |
| GEX | 10% | 22% |
| MOPI | 30% | supprime |

Le denominateur de confiance passe de 4/4 a 3/3.
