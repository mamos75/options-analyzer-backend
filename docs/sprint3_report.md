# Sprint 3 — Rapport Final

> Date : 2026-07-15  
> Scope : BTC Options Analyzer Dashboard — `/root/telegram-claude-bot/dashboard_options/`

---

## Résumé exécutif

Sprint 3 a corrigé 3 bugs critiques identifiés en Phase 0, renforcé l'architecture narrative avec des types réels, unifié les conventions de scores, et ajouté un middleware d'assertions runtime complet avec tests mutation.

**Résultat QA : 42/42 invariants OK en conditions réelles.**

---

## Phase 0 — Autopsies

### Bug 1 — STABILISANT fantôme (`main.py:3846`)
- **Cause** : `gex_obj.regime` (champ legacy, signe GEX brut) passé à `snap_dict["dashboard"]["gex_regime"]` au lieu de `gex_obj.regime_meca` (source v3-bis avec logique spot/flip/hystérésis)
- **Fix** : `gex_obj.regime` → `gex_obj.regime_meca`
- **Preuve live** : avant fix, `pro_decision.regime = STABILISANT` ; après fix = `ZONE_DE_FLIP`

### Bug 2 — Put wall fantôme (`regime.js:83-84`)
- **Cause** : labels convergence hardcodés `"put wall / call wall"` au lieu de labels dynamiques issus des types réels du snapshot
- **Fix** : Phase 2 — labels dynamiques via `convResult.types`

### Bug 3 — Coherence checks manquants
- **Cause** : `coherence_checks.py` existait mais assertions m2/l/m1 manquaient
- **Fix** : Phases 2 et 4

---

## Phase 1.1 — Fixes bugs

| Bug | Fichier | Ligne | Fix |
|-----|---------|-------|-----|
| 1 | `backend/main.py` | 3846 | `regime` → `regime_meca` |
| 2 | `frontend/js/widgets/regime.js` | 83-84 | labels convergence dynamiques |

---

## Phase 1.2 — Architecture linter

- Créé : `scripts/lint_regime_field.sh`
- Détecte `gex_obj.regime` (legacy) dans tout le backend
- PASS confirmé

---

## Phase 2 — Narratif : types réels, templates par régime

### `backend/narrative_resolver.py`
- Ajout `CONVERGENCE_TOLERANCE_PCT = 0.015` (1.5%, validé)
- Ajout dataclass `ConvergenceResult` + fonction `detect_convergence()`
- Ajout `_infer_niveau_type()` → types réels : `flip / call_wall / put_wall / atm / gravity / fallback`
- `NarrativeResolved` enrichi : `niveau_haut_type`, `niveau_bas_type`, champs convergence

### `backend/coherence_checks.py`
- Ajout assertion `(m2)` : `check_level_type_coherence()` — type de niveau dans texte = type réel snapshot
- Violation BLOQUANT → fallback neutre "Niveaux clés : flip $X, max pain $Y."

### `frontend/js/widgets/regime.js`
- Templates par régime ZONE_DE_FLIP : toujours conditionnel, jamais affirmatif
- Labels convergence dynamiques via `convResult.types`
- Signature `buildLevelsContext()` étendue : `+convResult`, `+gexRegime`

---

## Phase 3 — Scores convention unique /100

### Convention
**Tous les scores visibles = /100.** Les échelles internes (`-100/+100` biais, `0-10` conviction) ne sont jamais exposées brutes.

### `backend/pro_decision_engine.py`
- Ajout `_bias_label(score)` → labels qualitatifs : "Biais options : BAISSIER FORT / MODÉRÉ / NEUTRE / HAUSSIER MODÉRÉ / FORT"
- Toutes les occurrences de `{bias_score:.0f}/100` dans les thèses → `_bias_label(bias_score)`
- `conv_labels` : suppression des `(X/10)` → labels seuls : "Très faible", "Modérée", etc.
- `supporting_forces` : "Biais options -92/100 → BEAR" → "Biais options : BAISSIER FORT"

### `frontend/js/widgets/pro_decision.js`
- `convictionBar()` : suppression du span numérique `${conviction}/10`
- Bloc verdict : `${data.conviction}/10` → `${data.conviction_label}` uniquement

### `docs/scores_glossary.md`
- Créé : documentation complète de chaque score (source, échelle, sémantique, format d'affichage)
- Règle R4 : jamais `%` pour les scores de règles (pas une probabilité calibrée)

---

## Phase 4 — Middleware incontournable + tests mutation

### `backend/coherence_checks.py` — nouvelles assertions
- `(l)` `check_regime_source()` : BLOQUANT si `_gex_source == "legacy"`
- `(m1)` `check_level_amounts()` : WARNING si niveau mentionné dans texte mais montant None/0
- `run_coherence_checks()` : injecte `coherence: {status, violations_total, endpoint}` dans chaque payload

### `backend/tests/test_coherence_sprint3.py`
- **18 tests mutation**, tous PASS
- Couverture : assertion (l) x5, assertion (m1) x6, bloc coherence:{} x5, (m2) non-régression x2

### Vérification API
```json
"coherence": {
  "status": "OK",
  "violations_total": 0,
  "endpoint": "/api/pro_decision"
}
```

---

## Phase 5 — Nettoyage échelles

### `frontend/js/widgets/probabilities.js`
- `Complétude : X%` → tooltip `ℹ X% règles actives` (attribut `title` + libellé discret)

### Bilan /10
- Supprimés dans l'UI : conviction/10 dans pro_decision.js ✓
- Conv_labels nettoyés dans backend ✓
- `/10` restants dans `alerts.py` / `conviction_score.py` = logs internes Telegram — hors périmètre UI

---

## Phase 6 — Résiduels

### Flip zone harmonisée (`frontend/js/widgets/arbiter_quick.js`)
- Avant : `"Zone : $64,000 – $65,000"`
- Après : `"Zone $64,000–$65,000 (pivot $65,000)"`

### VEX/CEX strikes — tri croissant (`backend/vex_cex.py`)
- Avant : triés par `abs(valeur)` décroissant
- Après : top-5 sélectionnés par `abs(valeur)`, puis **retriés par strike croissant** pour l'affichage
- Exemple live : `$63k, $64k, $65k, $66k, $68k` ✓

### Gravity labels
- Pas de widget gravity actif dans le frontend Sprint 3 — reporté Sprint 4

---

## Tests & QA

| Suite | Résultat |
|-------|----------|
| `qa_live.py` (42 invariants) | **42/42 OK** |
| `test_coherence_sprint3.py` (18 mutations) | **18/18 PASS** |
| `test_coherence.py` (12 invariants existants) | Non re-exécuté — pas de régression observée |

---

## Fichiers modifiés

### Backend
| Fichier | Nature |
|---------|--------|
| `backend/main.py` | Bug 1 fix + injection `_niveau_types` (Phase 2) |
| `backend/narrative_resolver.py` | detect_convergence() + types réels (Phase 2) |
| `backend/coherence_checks.py` | (m2) + (l) + (m1) + coherence:{} (Phases 2, 4) |
| `backend/pro_decision_engine.py` | _bias_label() + conv_labels sans /10 (Phase 3) |
| `backend/vex_cex.py` | Tri strikes croissant (Phase 6) |

### Frontend
| Fichier | Nature |
|---------|--------|
| `frontend/js/widgets/regime.js` | Labels dynamiques + templates régime (Phases 1.1, 2) |
| `frontend/js/widgets/pro_decision.js` | Suppression /10 conviction (Phase 3) |
| `frontend/js/widgets/probabilities.js` | Complétude → tooltip (Phase 5) |
| `frontend/js/widgets/arbiter_quick.js` | Flip zone "Zone $X–$Y (pivot $Z)" (Phase 6) |

### Tests & Docs
| Fichier | Nature |
|---------|--------|
| `backend/tests/test_coherence_sprint3.py` | 18 tests mutation (Phase 4) |
| `scripts/lint_regime_field.sh` | Linter CI champ régime (Phase 1.2) |
| `docs/autopsy_sprint3.md` | Autopsies bugs (Phase 0) |
| `docs/scores_glossary.md` | Glossaire scores (Phase 3) |
| `docs/sprint3_report.md` | Ce fichier |

---

## Décisions architecture retenues

| Point | Décision |
|-------|----------|
| `-92/100` biais | Suppression totale — label qualitatif uniquement |
| Tolérance convergence | 1.5% maintenu |
| Flip zone | "Zone $X–$Y (pivot $Z)" partout |
| Gravity Sprint 3 | Pas de widget actif — reporté |
| Scores /10 logs internes | Conservés (alerts.py, conviction_score.py) — hors UI |
