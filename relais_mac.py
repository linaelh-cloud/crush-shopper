"""
Crush Shopper – le relais Mac.

Certaines boutiques (ex. Nocibé) bloquent les ordinateurs de GitHub, mais laissent entrer mon Mac.
Ce petit robot tourne SUR MON MAC (toutes les 10 minutes quand il est allumé) :
  1. il récupère ma wishlist et la mémoire du robot sur GitHub ;
  2. il relit seulement les articles que GitHub n'arrive pas à lire (statut « bloque »,
     ou déjà lus par le Mac) ;
  3. il envoie l'alerte Telegram si un article sous Crush Alert est revenu ;
  4. il renvoie le résultat sur GitHub (etat.json, et wishlist.json si l'alerte s'éteint).
Le robot GitHub voit « lu par mon Mac » et ne relit pas ces articles pendant 1 h 30.

On ne contourne jamais une protection : si un site bloque aussi mon Mac, on le note et on passe.
Les clés sont lues dans crush-alert/app/config.json (sur mon Mac seulement, jamais publiées).

Lancer à la main :  .venv/bin/python crush-alert/crush-shopper/relais_mac.py
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

import surveille

DEPOT = "linaelh-cloud/crush-shopper"
API = "https://api.github.com"
CONFIG = Path(__file__).resolve().parent.parent / "app" / "config.json"


def guichet(cle, fichier, methode="GET", corps=None):
    req = urllib.request.Request(
        f"{API}/repos/{DEPOT}/contents/{fichier}", method=methode,
        data=json.dumps(corps).encode() if corps else None,
        headers={"Authorization": f"Bearer {cle}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def lire(cle, fichier):
    f = guichet(cle, fichier)
    return json.loads(base64.b64decode(f["content"])), f["sha"]


def ecrire(cle, fichier, donnees, sha, message):
    texte = json.dumps(donnees, ensure_ascii=False, indent=2) + "\n"
    guichet(cle, fichier, "PUT", {"message": message, "sha": sha,
                                   "content": base64.b64encode(texte.encode()).decode()})


def a_relire(avant):
    """Les articles que GitHub ne sait pas lire : bloqués chez lui, ou déjà pris en charge par le Mac."""
    return avant.get("statut") == "bloque" or bool(avant.get("par_mac"))


def main():
    reglages = json.loads(CONFIG.read_text(encoding="utf-8"))
    cle = (reglages.get("github_token") or "").strip()
    if not cle:
        sys.exit("Pas de clé GitHub dans crush-alert/app/config.json (champ github_token).")
    # Les messages : Telegram, avec les réglages de mon appli Mac (l'e-mail reste envoyé par GitHub).
    os.environ.setdefault("TELEGRAM_TOKEN", reglages.get("telegram_token", ""))
    os.environ.setdefault("TELEGRAM_CHAT_ID", str(reglages.get("telegram_chat_id", "")))

    try:
        wishlist, sha_w = lire(cle, "wishlist.json")
        etat, sha_e = lire(cle, "etat.json")
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub refuse la clé (code {e.code}) : mets une clé valide dans crush-alert/app/config.json.")

    a_faire = [a for a in wishlist.get("articles", []) if a_relire(etat.get(a["url"], {}))]
    if not a_faire:
        print("Rien à relire : GitHub lit tous les articles lui-même.")
        return
    print(f"Le Mac relit {len(a_faire)} article(s) que GitHub ne peut pas lire…")

    lus, eteintes = {}, []
    with sync_playwright() as pw:
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for a in a_faire:
            nouveau, bloque_aussi, eteinte = surveille.traiter(page, a, etat.get(a["url"], {}), par_mac=True)
            if nouveau is None:
                continue
            if bloque_aussi:
                nouveau["precision"] = "le site bloque aussi mon Mac"
                nouveau.pop("par_mac", None)
            lus[a["url"]] = nouveau
            if eteinte:
                eteintes.append(a["url"])
        navigateur.close()

    # On renvoie sur GitHub : on relit la version la plus récente juste avant (le robot GitHub a pu écrire),
    # on y remplace seulement nos articles, et on recommence si quelqu'un a écrit entre-temps.
    for essai in range(3):
        try:
            etat, sha_e = lire(cle, "etat.json")
            for url, e in lus.items():
                etat[url] = e
            ecrire(cle, "etat.json", etat, sha_e, "Relais Mac : articles lus par mon Mac")
            break
        except urllib.error.HTTPError as e:
            if e.code not in (409, 422):
                raise
    if eteintes:
        for essai in range(3):
            try:
                wishlist, sha_w = lire(cle, "wishlist.json")
                for a in wishlist.get("articles", []):
                    if a["url"] in eteintes:
                        a["crush_alert"] = False
                ecrire(cle, "wishlist.json", wishlist, sha_w, "Relais Mac : Crush Alert éteinte (message envoyé)")
                break
            except urllib.error.HTTPError as e:
                if e.code not in (409, 422):
                    raise
    print(f"Terminé ✅ {len(lus)} article(s) mis à jour sur GitHub par mon Mac.")


if __name__ == "__main__":
    main()
