# Glossaire des scores — BTC Options Analyzer

> Sprint 3 — 2026-07-15

## Convention unique

**Tous les scores visibles dans l'interface sont sur 100 (0–100).**  
Les scores internes sur d'autres échelles ne sont jamais exposés bruts à l'utilisateur.

---

## Scores exposés dans l'UI

### 1. Score de règles probabiliste (`dominant_prob`) — `/100`
- **Source** : `probability_engine.py` → `ProbabilityEngine`
- **Échelle** : 0–100 (entier)
- **Sémantique** : probabilité estimée que la direction dominante se confirme sur l'horizon considéré (4h / 24h / 72h)
- **Affiché** : widget Probabilités — `"BULL 78/100 (4h)"` ou `"BEAR 62/100 (24h)"`
- **Horizon déclaré** : toujours affiché avec le score

### 2. Confiance Arbiter (`global_confidence`) — `%`
- **Source** : `arbiter.py` → `compute_arbiter_confidence()`
- **Échelle** : 0–100 (%)
- **Sémantique** : niveau de convergence des moteurs (GEX, dealer, OI, vol). En dessous de 30% → état SUPPRESSED, aucun trade actionnable.
- **Affiché** : widget Arbiter + bloc Verdict Pro — `"Confiance Arbiter : 45%"`

### 3. Edge probabiliste (`edge_quality`) — label qualitatif
- **Source** : `probability_engine.py` → `_classify_edge()`
- **Valeurs** : `FORT` / `MODERE` / `FAIBLE` / `INEXISTANT`
- **Sémantique** : qualité de l'edge directionnel (différence dominant_prob vs 50%)
- **Affiché** : badge coloré dans le widget Probabilités

---

## Scores internes (jamais affichés bruts)

### 4. Biais directionnel options (`directional_bias.score`) — interne
- **Source** : `directional_bias.py` → `compute_directional_bias()`
- **Échelle interne** : –100 à +100 (positif = haussier, négatif = baissier)
- **Exposé dans l'UI** : **label qualitatif uniquement**
  - `>= +60` → `"Biais options : HAUSSIER FORT"`
  - `+25 à +59` → `"Biais options : HAUSSIER MODÉRÉ"`
  - `–24 à +24` → `"Biais options : NEUTRE"`
  - `–59 à –25` → `"Biais options : BAISSIER MODÉRÉ"`
  - `<= –60` → `"Biais options : BAISSIER FORT"`
- **Apparaît dans** : `supporting_forces` de `/api/pro_decision`
- **Raison de non-exposition brute** : l'échelle –100/+100 est une grandeur composite interne sans interprétation directe pour un trader. Le label qualitatif est suffisant et non ambigu.

### 5. Conviction Pro (`conviction`) — interne, affiché comme label
- **Source** : `pro_decision_engine.py` → `_compute_conviction()`
- **Échelle interne** : 0–10 (sommation de contributions)
- **Exposé dans l'UI** : `conviction_label` uniquement (ex : `"Conviction élevée"`)
- **Barre visuelle** : la barre de progression reste (proportion visuelle), sans chiffre
- **Raison** : le /10 crée une fausse précision — c'est un score de confluence, pas une probabilité

### 6. Gravity score (`gravity.score`) — interne
- **Source** : `gravity_engine.py`
- **Échelle interne** : 0–100 (compression globale du gamma)
- **Exposé** : affiché dans le widget Gravity — à documenter Phase 6

---

## Règle R4 — Pas de % pour les scores de règles

Les scores de règles probabilistes (0–100) représentent une mesure de confluence, **pas une probabilité statistique calibrée**. Ils ne doivent jamais être affichés avec le symbole `%` (qui impliquerait une calibration bayésienne).

Format correct : `"BULL 78/100"` ✓  
Format incorrect : `"BULL 78%"` ✗

---

## Moteurs distincts — ne pas confondre

| Score | Moteur | Ce qu'il mesure |
|-------|--------|-----------------|
| `dominant_prob /100` | `probability_engine` | Confluence règles de marché sur horizon donné |
| `global_confidence %` | `arbiter` | Convergence inter-moteurs (GEX, dealer, OI, vol) |
| `directional_bias` | `directional_bias` | Pression nette options (OI pondéré, walls, GEX) |
| `conviction 0–10` | `pro_decision_engine` | Confluence signaux pour décision pro |

Ces quatre grandeurs mesurent des choses **différentes** sur des **horizons différents** — elles ne sont pas redondantes.
