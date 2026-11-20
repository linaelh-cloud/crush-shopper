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
  5. note ce qu'il a vu dans etat.json (sa mémoire d'un passage à l'autre),
     avec le nom et la photo de chaque article, que la page wishlist en ligne affiche.

GitHub le lance toutes les 5 minutes (en vrai, plus irrégulier) ET dès que ma page
wishlist change wishlist.json (ajout d'un lien, Crush Alert, bouton « Vérifier »).

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


def envoyer_email(article, texte=None):
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
    mail.set_content(texte or message(article))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as serveur:
            serveur.login(adresse, mot_de_passe.replace(" ", ""))
            serveur.send_message(mail)
        return True
    except Exception as e:
        print(f"  e-mail raté ({e.__class__.__name__})")
        return False


def message(article, nom=""):
    option = f" – {article['option']}" if article.get("option") else ""
    return f"{ACCROCHE}\n{article.get('nom') or nom or 'Ton article'}{option}\n{article['url']}"


def teinte_de(a):
    """La teinte MAC : rangée dans la liste, sinon lue dans le lien (…?shade=Modesty)."""
    if a.get("teinte"):
        return a["teinte"]
    m = re.search(r"[?&]shade=([^&#]+)", a.get("url", ""))
    return urllib.parse.unquote_plus(m.group(1)) if m else ""


def traiter(page, a, avant, i=0, par_mac=False):
    """Lit UN article et décide de l'alerte. Rend (nouvelle mémoire ou None, site bloqué ?, alerte éteinte ?).
    Utilisé par le robot GitHub ET par le relais Mac (relais_mac.py)."""
    print(f"→ {a.get('nom') or avant.get('nom') or a['url'][:60]} ({a.get('option', '')})")
    try:
        reponse = page.goto(a["url"], wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(4000)
    except Exception as e:
        print(f"  page impossible à ouvrir ({e.__class__.__name__})")
        if not avant.get("statut"):  # nouvel article : on le dit à la page, pour ne pas la laisser attendre
            return dict(avant, statut="inconnu", precision="page impossible à ouvrir", depuis=maintenant()), False, False
        return None, False, False
    code = reponse.status if reponse else 0
    if not par_mac:
        try:  # la photo sert juste à vérifier : elle ne doit jamais bloquer le robot
            CAPTURES.mkdir(exist_ok=True)
            page.screenshot(path=str(CAPTURES / f"article-{i + 1}.png"), timeout=10_000)
        except Exception:
            print("  (capture d'écran ratée, on continue)")
    if sites.page_bloquee(page.title(), code):
        print(f"  ⛔ Le site a refusé l’accès au robot (code {code})")
        return dict(avant, statut="bloque", precision="le site bloque les robots",
                    depuis=avant.get("depuis") if avant.get("statut") == "bloque" else maintenant()), True, False

    # Le bon lecteur selon le site : MAC (appris) ou universel (fiche standard)
    infos = sites.lire(a["url"], page.content(), a.get("option", ""), teinte_de(a))
    statut = infos["statut"]
    print(f"  statut : {statut}")
    nouveau = dict(avant, statut=statut)
    if avant.get("statut") != statut:
        nouveau["depuis"] = maintenant()
    nouveau.pop("verifie_le", None)
    # Le nom, la photo, la marque et le prix servent à la page wishlist en ligne.
    for cle in ("nom", "photo", "precision", "marque", "prix"):
        if infos.get(cle):
            nouveau[cle] = infos[cle]
    if par_mac:
        nouveau["par_mac"] = datetime.now(timezone.utc).isoformat()   # « lu par mon Mac » (relais Mac)
    else:
        nouveau.pop("par_mac", None)

    eteinte = False
    revenu = avant.get("statut") == sites.RUPTURE and statut == sites.DISPO
    if a.get("crush_alert") and statut == sites.DISPO and (revenu or not avant.get("alerte_envoyee")):
        texte = message(a, nouveau.get("nom", ""))
        telegram = envoyer_telegram(texte)
        email = envoyer_email(a, texte)
        print(f"  💌 Crush Alert : Telegram={'oui' if telegram else 'non'}, e-mail={'oui' if email else 'non'}")
        if telegram or email:
            nouveau["alerte_envoyee"] = True
            # Mission accomplie : Crush Alert s'éteint toute seule pour cet article.
            a["crush_alert"] = False
            nouveau["eteinte_le"] = maintenant()
            eteinte = True
            print("  🔕 Crush Alert éteinte toute seule (message envoyé)")
    # On efface la coche « déjà prévenue » quand l'article repart en rupture,
    # ou quand Crush Alert est éteinte : si je la rallume, le robot est prêt à prévenir.
    if statut == sites.RUPTURE or not a.get("crush_alert"):
        nouveau["alerte_envoyee"] = False
    return nouveau, False, eteinte


def lu_par_mac_recemment(avant, minutes=90):
    """Le relais Mac a lu cet article il y a moins d'1 h 30 : le robot GitHub ne le relit pas
    (le site le bloquerait et effacerait la bonne lecture du Mac)."""
    try:
        quand = datetime.fromisoformat(avant.get("par_mac", ""))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - quand).total_seconds() < minutes * 60


def main():
    wishlist = lire_json(WISHLIST, {"articles": []})
    articles = wishlist["articles"]
    etat = lire_json(ETAT, {})
    CAPTURES.mkdir(exist_ok=True)
    bloque = False
    eteintes = []  # liens des articles dont Crush Alert s'est éteinte pendant ce passage

    with sync_playwright() as pw:
        # Fenêtre visible (sur GitHub, dans un « écran imaginaire ») : MAC refuse les navigateurs invisibles.
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for i, a in enumerate(articles):
            avant = etat.get(a["url"], {})
            if lu_par_mac_recemment(avant):
                print(f"→ {avant.get('nom') or a['url'][:60]} : lu par mon Mac il y a peu (relais Mac), on le laisse")
                continue
            nouveau, est_bloque, eteinte = traiter(page, a, avant, i)
            if nouveau is not None:
                etat[a["url"]] = nouveau
            bloque = bloque or est_bloque
            if eteinte:
                eteintes.append(a["url"])
        navigateur.close()

    # On oublie les articles que j'ai retirés de ma wishlist.
    liens = {a["url"] for a in articles}
    etat = {url: e for url, e in etat.items() if url in liens}
    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding="utf-8")
    if eteintes:
        WISHLIST.write_text(json.dumps(wishlist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Gardé de côté : si la page a changé la liste pendant ce passage, on réappliquera ces extinctions.
    (CAPTURES / "eteintes.json").write_text(json.dumps(eteintes), encoding="utf-8")
    if bloque:
        # On le signale, sans faire échouer tout le lancement : les autres sites ont bien été lus.
        print("⚠️ Au moins un site bloque les ordinateurs de GitHub (voir « Le site a refusé » plus haut).")
        return
    print("Résultat : le robot a pu lire les sites depuis GitHub ✅")


def reappliquer():
    """Appelé seulement si j'ai changé ma liste (depuis la page) pendant que le robot travaillait :
    on repart de la liste la plus récente et on y réapplique les Crush Alert éteintes."""
    eteintes = set(lire_json(CAPTURES / "eteintes.json", []))
    wishlist = lire_json(WISHLIST, {"articles": []})
    for a in wishlist["articles"]:
        if a["url"] in eteintes:
            a["crush_alert"] = False
    WISHLIST.write_text(json.dumps(wishlist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Liste la plus récente reprise, {len(eteintes)} extinction(s) réappliquée(s).")


def maintenant():
    return datetime.now(timezone.utc).strftime("%d/%m %H:%M (heure UTC)")


if __name__ == "__main__":
    reappliquer() if "--reappliquer" in sys.argv else main()
