# Règles de développement — dashboard_options

## VPS
SSH : root@138.68.80.156
Projet : /root/telegram-claude-bot/dashboard_options/
Docker rebuild : `docker compose build backend && docker compose up -d backend`
Push : `git push origin main`

## Règle fix runtime
Toute annonce de correction d'une erreur runtime DOIT inclure la preuve log :
  grep count avant : X
  grep count après : 0
"Corrigé en FX" sans log = non corrigé.

## Ordre d'exécution F10/F11
F10.1 → F10.2 → [rebuild] → F10.3 → F10.4 → F10.5 → F10.6 → [rebuild]
→ F11.1 → F11.2 → [rebuild] → F11.3 → F11.4 → F11.5 → CLAUDE.md
