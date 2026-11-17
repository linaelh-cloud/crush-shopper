# Crush Shopper · le robot en ligne

L'assistant de ta wishlist. Ce dossier fait tourner le robot de surveillance sur les ordinateurs gratuits de GitHub (GitHub Actions), même quand le Mac est éteint.

- `wishlist.json` : les articles à surveiller (« crush_alert »: true = surveillé).
- `surveille.py` : le robot (lit le stock de la bonne couleur, envoie « Crush Alert 💌 » sur Telegram).
- `sites.py` : le « lecteur » des pages MAC.
- `.github/workflows/surveillance.yml` : la recette que GitHub suit pour lancer le robot.
- `etat.json` : la mémoire du robot d'un passage à l'autre.

Les clés Telegram sont rangées dans les « Secrets » du dépôt, jamais dans les fichiers.
