"""
Crush Shopper – le robot « en ligne » (version GitHub).

GitHub lance ce fichier tout seul, régulièrement, sur un de ses ordinateurs.
À chaque passage, le robot :
  1. lit la wishlist (wishlist.json) ;
  2. ouvre la page de chaque article dans Chrome ;
  3. lit si la couleur voulue est disponible ;
  4. si un article sous Crush Alert est revenu en stock : envoie le message
     « Crush Alert 💌 - De retour dans ton panier ! » sur Telegram ;
  5. note ce qu'il a vu dans etat.json (sa mémoire d'un passage à l'autre).

Les clés Telegram ne sont PAS dans ce fichier : GitHub les donne en secret
(variables TELEGRAM_TOKEN et TELEGRAM_CHAT_ID).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import sites

DOSSIER = Path(__file__).parent
WISHLIST = DOSSIER / "wishlist.json"
ETAT = DOSSIER / "etat.json"
CAPTURES = DOSSIER / "captures"
ACCROCHE = "Crush Alert 💌 - De retour dans ton panier !"


def lire_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def envoyer_telegram(texte):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("  (pas de clé Telegram : message non envoyé)")
        return False
    donnees = urllib.parse.urlencode({"chat_id": chat, "text": texte}).encode()
    for essai in range(3):  # la connexion coupe parfois : on réessaie
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        data=donnees, timeout=15) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except Exception as e:
            print(f"  Telegram, essai {essai + 1} raté ({e.__class__.__name__})")
        time.sleep(3)
    return False


def message(article):
    option = f" – {article['option']}" if article.get("option") else ""
    return f"{ACCROCHE}\n{article['nom']}{option}\n{article['url']}"


def main():
    articles = lire_json(WISHLIST, {"articles": []})["articles"]
    etat = lire_json(ETAT, {})
    CAPTURES.mkdir(exist_ok=True)
    bloque = False

    with sync_playwright() as pw:
        # Fenêtre visible (sur GitHub, dans un « écran imaginaire ») : MAC refuse les navigateurs invisibles.
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for i, a in enumerate(articles):
            avant = etat.get(a["url"], {})
            print(f"→ {a['nom']} ({a.get('option', '')})")
            try:
                reponse = page.goto(a["url"], wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(4000)
            except Exception as e:
                print(f"  page impossible à ouvrir ({e.__class__.__name__})")
                continue
            code = reponse.status if reponse else 0
            page.screenshot(path=str(CAPTURES / f"article-{i + 1}.png"))
            if code == 403 or "access denied" in page.title().lower():
                print(f"  ⛔ MAC a refusé l'accès au robot (code {code})")
                etat[a["url"]] = dict(avant, statut="bloque", verifie_le=maintenant())
                bloque = True
                continue

            infos = sites.lire_mac(page.content(), a.get("teinte", ""))
            statut = infos["statut"]
            print(f"  statut : {statut}")
            nouveau = dict(avant, statut=statut, verifie_le=maintenant())

            revenu = avant.get("statut") == sites.RUPTURE and statut == sites.DISPO
            if a.get("crush_alert") and statut == sites.DISPO and (revenu or not avant.get("alerte_envoyee")):
                if envoyer_telegram(message(a)):
                    print("  💌 Crush Alert envoyée sur Telegram")
                    nouveau["alerte_envoyee"] = True
            if statut == sites.RUPTURE:
                nouveau["alerte_envoyee"] = False
            etat[a["url"]] = nouveau
        navigateur.close()

    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding="utf-8")
    if bloque:
        print("Résultat : MAC bloque les ordinateurs de GitHub.")
        sys.exit(2)
    print("Résultat : le robot a pu lire MAC depuis GitHub ✅")


def maintenant():
    return datetime.now(timezone.utc).strftime("%d/%m %H:%M (heure UTC)")


if __name__ == "__main__":
    main()
