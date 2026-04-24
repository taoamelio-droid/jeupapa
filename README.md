# Jeu pour Papa

Petit jeu de puzzle (glissade parallele de pieces) servi par un serveur HTTP Python stdlib.

## Lancer en local

```bash
python3 papa_cursor_app_v2.py
```

Le jeu ecoute sur `http://127.0.0.1:8000`. Depuis le meme Wi-Fi, il affiche
aussi une URL `http://<ip-locale>:8000` utilisable depuis un telephone.

## Regenerer les niveaux

```bash
rm -f levels.json && PAPA_FORCE_REBUILD_LEVELS=1 python3 papa_cursor_app_v2.py
```

## Fichiers

- `papa_cursor_app_v2.py` — serveur HTTP + logique du jeu + generateur de niveaux (BFS).
- `levels.json` — niveaux pre-calcules (regenere automatiquement si sa signature ne correspond plus au code).
- `papa_puzzle_progress.json` — progression locale du joueur (NON commite).
