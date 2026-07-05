# Options Analyzer — Frontend

Dashboard temps réel BTC options. Monolithe → ES modules (Phase 5).

## Structure

```
frontend/
├── options_analyzer.html   HTML shell (−70% vs monolithe)
└── js/
    ├── main.js             Entry point — DOMContentLoaded + window.* onclick
    ├── config.js           CFG (seuils), API_BASE, REFRESH_INTERVAL
    ├── store.js            État partagé (authState, lastRegime, timers…)
    ├── api.js              apiFetch(endpoint, signal) — AbortController
    ├── auth.js             Patreon OAuth + JWT legacy + screens
    ├── scheduler.js        loadAllData() + refresh loop + visibilitychange
    ├── lib/
    │   ├── stats.js        wilsonLB(), clientHasEdge() — miroir backend
    │   ├── fmt.js          esc(), fmtPrice(), tagBadge(), fmtBig()
    │   └── canvas.js       drawSparkline(), drawDualAxis(), drawVcLine()
    └── widgets/
        ├── signal.js        M1 — Signal principal
        ├── levels.js        M2 — Niveaux clés (walls)
        ├── probabilities.js M3 — Probabilités haussier/baissier
        ├── context.js       M4 — Contexte (vol, dealer, MOPI, GEX)
        ├── narrative.js     M5 — Narrative IA
        ├── model.js         M6 — Modèle actif (leaderboard)
        ├── vol_weather.js   M7 — Météo volatilité
        ├── gex_dex.js       M8 — GEX & DEX évolution
        ├── mopi_btc.js      M9 — MOPI vs BTC
        ├── regime.js        Régime de marché (64-cas classifier)
        └── vex_cex.js       VEX/CEX — Vanna & Charm Exposure
```

## Corrections appliquées (Phases 1-6)

| ID | Sévérité | Description | Fichier |
|----|----------|-------------|---------|
| M7 | Medium | `fmtPrice()` précision adaptative (0/1/2 dp selon magnitude) | `lib/fmt.js` |
| C4 | Critical | Race condition refresh : `AbortController` + sequence token | `api.js`, `scheduler.js` |
| E1 | High | `Promise.all` → `Promise.allSettled` (résilience multi-endpoint) | `scheduler.js` |
| E2 | High | Données périmées : `data-stale="1"` + badge amber CSS | `scheduler.js` |
| M4 | Medium | Scheduler timestamp-based + `visibilitychange` (tab sleep) | `scheduler.js` |
| E3 | High | XSS : `esc()` sur tous les champs API string dans `innerHTML` | `lib/fmt.js` + widgets |
| E6 | High | GEX=0 (Gamma Flip) : cas neutre distinct de GEX positif | `widgets/gex_dex.js` |
| M3 | Medium | try/catch par widget — une erreur JS n'arrête pas les autres | tous les widgets |
| E4 | High | `wilsonLB()` client-side — miroir `backend/wilson_utils.py` | `lib/stats.js` |
| M8 | Medium | `CFG` — tous les seuils en un seul endroit | `config.js` |
| M2 | Medium | `tagBadge()` — rendu unifié des tags walls | `lib/fmt.js` |

## Développement

### Mock server (Phase 0)
```bash
python3 mock/server.py   # port 8765
```

### Tests
```bash
node --test tests/test_mock.js    # tests mock server + structure HTML
node --test tests/test_logic.js   # tests logique pure JS (stats, fmt, payload)
```

### Ajouter un widget
1. Créer `js/widgets/mon_widget.js` avec `export async function loadMonWidget(signal) { ... }`
2. Importer dans `scheduler.js` et ajouter à `Promise.allSettled([...])`
3. Ajouter le `<div id="mon-widget-content">` dans le HTML
4. Mettre à jour `API_CONTRACT.md`

## Sécurité

- **XSS** : `esc()` obligatoire pour tout champ API dans `innerHTML`
- **Auth** : Patreon OAuth (Supabase) + JWT legacy — token stocké en `localStorage`
- **CORS** : API sur même origin — pas de credentials cross-origin
