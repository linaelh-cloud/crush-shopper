"""
Crush Shopper – le robot « en ligne » (version GitHub).

GitHub lance ce fichier tout seul, régulièrement, sur un de ses ordinateurs.
À chaque passage, le robot :
  1. lit la wishlist (wishlist.json) ;
  2. ouvre la page de chaque article dans Chrome ;
  3. lit si la couleur voulue est disponible ;
  4. si un article sous Crush Alert est revenu en stock : envoie le message
     « Crush Alert 💌 - De retour dans ton panier ! » sur Telegram et par e-mail,
     puis éteint Crush Alert pour cet article (mission accomplie) ;
  5. note ce qu'il a vu dans etat.json (sa mémoire d'un passage à l'autre).

Les clés Telegram ne sont PAS dans ce fichier : GitHub les donne en secret
(variables TELEGRAM_TOKEN et TELEGRAM_CHAT_ID).
"""
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
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
    # On retrouve la clé et le numéro même s'il y a du texte ou des espaces autour
    # (par exemple si tout le message de BotFather a été collé dans le coffre-fort).
    brut_token = os.environ.get("TELEGRAM_TOKEN", "")
    brut_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    m_token = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", brut_token)
    m_chat = re.search(r"-?\d{5,}", brut_chat)
    if not m_token or not m_chat:
        print(f"  Telegram : clé trouvée = {'oui' if m_token else 'NON'}, "
              f"numéro de conversation trouvé = {'oui' if m_chat else 'NON'} "
              "→ vérifier les secrets TELEGRAM_TOKEN et TELEGRAM_CHAT_ID")
        return False
    token, chat = m_token.group(0), m_chat.group(0)
    donnees = urllib.parse.urlencode({"chat_id": chat, "text": texte}).encode()
    for essai in range(3):  # la connexion coupe parfois : on réessaie
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        data=donnees, timeout=15) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except urllib.error.HTTPError as e:
            # Telegram explique pourquoi il refuse (jamais la clé n'est affichée)
            try:
                raison = json.loads(e.read()).get("description", "")
            except Exception:
                raison = ""
            print(f"  Telegram refuse (essai {essai + 1}) : {e.code} {raison}")
            if e.code in (400, 401, 403, 404):
                return False  # inutile de réessayer : c'est un problème de clé ou de conversation
        except Exception as e:
            print(f"  Telegram, essai {essai + 1} raté ({e.__class__.__name__})")
        time.sleep(3)
    return False


def envoyer_email(article):
    """Envoie l'alerte par e-mail avec Gmail. L'adresse et le mot de passe d'application
    viennent du coffre-fort de GitHub (EMAIL_ADRESSE, EMAIL_MOT_DE_PASSE)."""
    adresse = os.environ.get("EMAIL_ADRESSE", "")
    mot_de_passe = os.environ.get("EMAIL_MOT_DE_PASSE", "")
    if not adresse or not mot_de_passe:
        print("  (pas d'adresse e-mail : e-mail non envoyé)")
        return False
    mail = EmailMessage()
    mail["Subject"] = ACCROCHE
    mail["From"] = f"Crush Shopper <{adresse}>"
    # Destinataire : EMAIL_DESTINATAIRE si on l'a rangé (envoi depuis l'adresse de Crush Shopper),
    # sinon on s'écrit à soi-même (Gmail affiche alors « me »).
    mail["To"] = os.environ.get("EMAIL_DESTINATAIRE", "").strip() or adresse
    mail.set_content(message(article))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as serveur:
            serveur.login(adresse, mot_de_passe.replace(" ", ""))
            serveur.send_message(mail)
        return True
    except Exception as e:
        print(f"  e-mail raté ({e.__class__.__name__})")
        return False


def message(article):
    option = f" – {article['option']}" if article.get("option") else ""
    return f"{ACCROCHE}\n{article['nom']}{option}\n{article['url']}"


def main():
    wishlist = lire_json(WISHLIST, {"articles": []})
    articles = wishlist["articles"]
    etat = lire_json(ETAT, {})
    CAPTURES.mkdir(exist_ok=True)
    bloque = False
    wishlist_changee = False

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
            try:  # la photo sert juste à vérifier : elle ne doit jamais bloquer le robot
                page.screenshot(path=str(CAPTURES / f"article-{i + 1}.png"), timeout=10_000)
            except Exception:
                print("  (capture d'écran ratée, on continue)")
            if sites.page_bloquee(page.title(), code):
                print(f"  ⛔ Le site a refusé l’accès au robot (code {code})")
                etat[a["url"]] = dict(avant, statut="bloque", depuis=avant.get("depuis") if avant.get("statut") == "bloque" else maintenant())
                bloque = True
                continue

            # Le bon lecteur selon le site : MAC (appris) ou universel (fiche standard)
            infos = sites.lire(a["url"], page.content(), a.get("option", ""), a.get("teinte", ""))
            statut = infos["statut"]
            print(f"  statut : {statut}")
            # La mémoire ne change que si l'état change : pas de sauvegarde inutile toutes les 15 min.
            nouveau = dict(avant, statut=statut)
            if avant.get("statut") != statut:
                nouveau["depuis"] = maintenant()
            nouveau.pop("verifie_le", None)

            revenu = avant.get("statut") == sites.RUPTURE and statut == sites.DISPO
            if a.get("crush_alert") and statut == sites.DISPO and (revenu or not avant.get("alerte_envoyee")):
                telegram = envoyer_telegram(message(a))
                email = envoyer_email(a)
                print(f"  💌 Crush Alert : Telegram={'oui' if telegram else 'non'}, e-mail={'oui' if email else 'non'}")
                if telegram or email:
                    nouveau["alerte_envoyee"] = True
                    # Mission accomplie : Crush Alert s'éteint toute seule pour cet article.
                    # On l'écrit dans la wishlist (le robot ne surveille plus) et dans la mémoire
                    # (« eteinte_le ») pour que l'appli du Mac éteigne aussi le bouton.
                    a["crush_alert"] = False
                    nouveau["eteinte_le"] = maintenant()
                    wishlist_changee = True
                    print("  🔕 Crush Alert éteinte toute seule (message envoyé)")
            # On efface la coche « déjà prévenue » quand l'article repart en rupture,
            # ou quand Crush Alert est éteinte : si je la rallume, le robot est prêt à prévenir.
            if statut == sites.RUPTURE or not a.get("crush_alert"):
                nouveau["alerte_envoyee"] = False
            etat[a["url"]] = nouveau
        navigateur.close()

    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding="utf-8")
    if wishlist_changee:
        WISHLIST.write_text(json.dumps(wishlist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if bloque:
        # On le signale, sans faire échouer tout le lancement : les autres sites ont bien été lus.
        print("⚠️ Au moins un site bloque les ordinateurs de GitHub (voir « Le site a refusé » plus haut).")
        return
    print("Résultat : le robot a pu lire les sites depuis GitHub ✅")


def maintenant():
    return datetime.now(timezone.utc).strftime("%d/%m %H:%M (heure UTC)")


if __name__ == "__main__":
    main()
