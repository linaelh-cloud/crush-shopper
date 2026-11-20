"""
Crush Shopper – le robot « Pour toi » (suggestions de la styliste IA).

L'ancien « cerveau gratuit » (recherche sur Zalando + règles) a été retiré le 18/11 :
pas assez adapté ni personnalisé. Maintenant, seule une styliste IA propose des idées.

D'où viennent les idées (dans cet ordre) :
  A. CRUSH STYLISTE (la routine Claude de mon abonnement, sans facture en plus) : chaque matin elle cherche
     sur tout le web et écrit styliste.json dans le dépôt (voir styliste/CONSIGNE.md) ;
  B. sinon, la clé payante ANTHROPIC_API_KEY (ia.py), si elle est rangée dans le coffre-fort de GitHub.
Puis le robot ouvre chaque lien et vérifie photo, prix et stock, et range tout dans docs/suggestions.json,
que l'appli affiche dans l'onglet « POUR TOI ».

Pas d'idées nouvelles : on garde celles d'avant (au début : l'appli affiche « Ta styliste arrive bientôt »).
Avec la clé payante : si ma wishlist n'a pas changé depuis moins de 3 jours, pas de nouvelle recherche.
On ne contourne jamais une protection : si un site refuse le robot, on laisse tomber ce lien.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import ia
import sites

DOSSIER = Path(__file__).parent
WISHLIST = DOSSIER / "wishlist.json"
ETAT = DOSSIER / "etat.json"
SORTIE = DOSSIER / "docs" / "suggestions.json"
STYLISTE = DOSSIER / "styliste.json"      # les idées écrites par la routine Crush Styliste
SECOURS = DOSSIER / "docs" / "secours.json"   # photos de secours des articles dont le site bloque le robot
BF_WEB = DOSSIER / "docs" / "bf-web.json"     # offres Black Friday trouvées par la styliste ailleurs (presse, Insta, TikTok)
TYPES_SOURCE = {"site de la marque", "presse", "site de bons plans", "Instagram", "TikTok", "newsletter", "autre"}
EN_TOUT = 40          # idées affichées au maximum


def lire_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def base(url):
    """L'adresse d'un produit sans ses « ?… » : pour reconnaître le même produit."""
    u = urlparse(url)
    return f"{u.netloc}{u.path}".rstrip("/").lower()


def ecrire(donnees):
    donnees["mise_a_jour"] = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    SORTIE.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verifier(page, idee, sources):
    """Le robot ouvre le lien donné par la styliste : la fiche existe-t-elle ? photo, prix, stock ?
    (Un site qui bloque les robots : on laisse tomber, on ne force jamais.)"""
    lien = (idee.lien or "").strip()
    if not lien.startswith("http"):
        return None
    try:
        r = page.goto(lien, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2500)
        if not r or r.status >= 400 or sites.page_bloquee(page.title(), r.status):
            return None
        infos = sites.lire(page.url, page.content())
    except Exception:
        return None
    if not infos.get("photo"):
        return None
    prix = infos.get("prix") or ""
    if prix and (not prix.endswith("€") or prix.startswith("≈")):
        return None   # seulement des boutiques qui vendent en euros (pas de dollars ni de prix convertis)
    if not prix and (idee.prix or "").strip().endswith("€"):
        infos["prix"] = idee.prix.strip()   # certaines fiches (ex. Zalando) n'écrivent pas le prix : on garde celui vu par la styliste
    src = sources.get(idee.pour_url) or next(iter(sources.values()))
    v = {"nom": idee.nom + (f" - {idee.teinte_ou_couleur}" if idee.teinte_ou_couleur else ""),
         "lien": page.url.split("#")[0], "photo": infos["photo"], "prix": infos.get("prix") or idee.prix,
         "marque": idee.marque or infos.get("marque", ""), "boutique": urlparse(page.url).netloc.replace("www.", ""),
         "genre": idee.genre, "raison": idee.pourquoi, "choisi_par_ia": True,
         "pour": src["url"], "categorie": src["categorie"], "statut": infos.get("statut", "")}
    if infos.get("statut") == sites.RUPTURE:
        v["raison"] += " · en rupture pour l'instant"
    return v


def maj_secours(routine, articles):
    """Photos de secours : pour un article dont le site bloque le robot, la styliste a trouvé le MÊME produit
    sur un autre site qui accepte les robots. Le robot ouvre ce lien et garde la photo, le nom, la marque et le prix
    (dans docs/secours.json, que l'appli lit). Le site qui bloque n'est jamais visité."""
    avant = lire_json(SECOURS, {})
    if routine.get("le") and avant.get("_le") == routine.get("le"):
        return
    liens_wishlist = {a["url"] for a in articles}
    secours = {u: v for u, v in avant.items() if u in liens_wishlist}      # on oublie les articles retirés
    a_lire = [x for x in routine.get("secours", []) if x.get("pour_url") in liens_wishlist]
    if not a_lire:
        return
    with sync_playwright() as pw:
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for x in a_lire:
            lien = (x.get("lien") or "").strip()
            if not lien.startswith("http"):
                secours[x["pour_url"]] = {"note": x.get("note") or "produit introuvable ailleurs", "nom": x.get("nom", ""),
                                          "marque": x.get("marque", "")}
                continue
            try:
                r = page.goto(lien, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2500)
                if not r or r.status >= 400 or sites.page_bloquee(page.title(), r.status):
                    continue
                infos = sites.lire(page.url, page.content())
            except Exception:
                continue
            if not infos.get("photo"):
                continue
            secours[x["pour_url"]] = {"photo": infos["photo"], "nom": x.get("nom") or infos.get("nom", ""),
                                      "marque": x.get("marque") or infos.get("marque", ""), "prix": infos.get("prix", ""),
                                      "lien": page.url.split("#")[0], "source": urlparse(page.url).netloc.replace("www.", "")}
            print(f"🖼️ Photo de secours trouvée sur {secours[x['pour_url']]['source']} pour {x['pour_url'][:60]}")
        navigateur.close()
    secours["_le"] = routine.get("le", "")
    SECOURS.write_text(json.dumps(secours, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def maj_bf_web(routine):
    """Offres Black Friday que mon robot n'a pas pu lire sur le site de la marque (site qui bloque, offre pas
    encore sur la page…) : Crush Styliste les a trouvées dans la presse, sur Instagram, TikTok ou un site de bons plans.
    On les range dans docs/bf-web.json (l'appli les affiche avec leur source). On garde une offre 3 jours au plus."""
    from datetime import datetime, timedelta, timezone
    trouvees = routine.get("black_friday") or []
    avant = lire_json(BF_WEB, {})
    if not trouvees or avant.get("_le") == routine.get("le"):
        return
    limite = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    offres = {k: v for k, v in avant.items() if not k.startswith("_") and (v.get("vu_le") or "") >= limite}
    jour = datetime.now(timezone.utc).date().isoformat()
    for x in trouvees:
        marque, offre, source = (x.get("marque") or "").strip(), (x.get("offre") or "").strip(), (x.get("source") or "").strip()
        # Jamais sans source vérifiable : une offre doit venir d'un lien réel.
        if not marque or not offre or not source.startswith("http") or len(offre) > 140:
            continue
        ancien = offres.get(marque.lower(), {})
        offres[marque.lower()] = {
            "marque": marque, "offre": offre, "source": source,
            "type": x.get("type") if x.get("type") in TYPES_SOURCE else "autre",
            "en_cours": bool(x.get("en_cours")), "debut": x.get("debut", ""), "fin": x.get("fin", ""),
            "code": (x.get("code") or "").strip()[:20],
            "vu_le": jour,
            # « nouveau » seulement si l'offre a changé depuis la dernière fois
            "nouveau_le": ancien.get("nouveau_le", "") if ancien.get("offre") == offre else jour,
        }
        print(f"📰 Black Friday {marque} : {offre} ({offres[marque.lower()]['type']})")
    offres["_le"] = routine.get("le", "")
    BF_WEB.write_text(json.dumps(offres, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    liste = lire_json(WISHLIST, {"articles": []})
    articles, marques_aimees = liste.get("articles", []), liste.get("marques_aimees", [])
    etat = lire_json(ETAT, {})
    avant = lire_json(SORTIE, {})

    # A. De nouvelles idées de la routine Crush Styliste ?
    routine = lire_json(STYLISTE, {})
    maj_secours(routine, articles)
    maj_bf_web(routine)
    nouvelles = bool(routine.get("idees")) and routine.get("le") != avant.get("styliste_le")
    if not nouvelles and not ia.active():
        if "suggestions" not in avant:
            ecrire({"par_ia": False, "mon_style": "", "suggestions": []})
        print("Pas de nouvelles idées de Crush Styliste (et pas de clé payante) : on garde les idées d'avant.")
        return

    sources = {}
    for a in articles:
        e = etat.get(a["url"], {})
        sources[a["url"]] = {"url": a["url"], "nom": e.get("nom") or a.get("nom") or "article",
                             "option": a.get("option", ""), "marque": e.get("marque", ""), "prix": e.get("prix", ""),
                             "categorie": a.get("categorie") or ("Beauté" if "maccosmetics" in a["url"] else "Mode")}
    if not sources:
        ecrire({"par_ia": True, "mon_style": "", "suggestions": []})
        return

    empreinte = "|".join(sorted(f"{a['url']}#{a.get('option', '')}" for a in articles)) + "|" + ",".join(marques_aimees)
    if nouvelles:
        try:
            sel = ia.Selection(style=routine.get("style", ""), idees=routine["idees"])
        except Exception as ex:
            print(f"styliste.json mal rempli ({ex.__class__.__name__}) : on garde les idées d'avant.")
            return
        quand = routine.get("le", "")
        print(f"💌 Crush Styliste ({quand[:16]}) : {len(sel.idees)} idées · style = {sel.style}")
    else:
        # B. La clé payante. Pour ne pas payer pour rien : wishlist inchangée depuis moins de 3 jours = on garde.
        try:
            recente = (datetime.now(timezone.utc) - datetime.fromisoformat(avant.get("styliste_le", ""))).days < 3
        except ValueError:
            recente = False
        if avant.get("empreinte") == empreinte and recente and avant.get("suggestions"):
            print(f"Wishlist inchangée : on garde les idées de la styliste du {avant['styliste_le'][:10]}.")
            return
        sel = ia.styliste_web([{"url": s["url"], "nom": s["nom"], "option": s["option"], "marque": s["marque"],
                                "prix": s["prix"], "categorie": s["categorie"]} for s in sources.values()], marques_aimees)
        if not sel or not sel.idees:
            print("La styliste n'a rien renvoyé : on garde les idées d'avant.")
            return
        quand = datetime.now(timezone.utc).isoformat()
        print(f"🤖 Styliste (clé) : {len(sel.idees)} idées trouvées sur le web · style = {sel.style}")

    deja = {base(a["url"]) for a in articles}
    gardees, vues = [], set()
    with sync_playwright() as pw:
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for idee in sel.idees:
            v = verifier(page, idee, sources)
            if v and base(v["lien"]) not in deja and base(v["lien"]) not in vues:
                vues.add(base(v["lien"]))
                gardees.append(v)
        navigateur.close()
    print(f"🔍 Liens vérifiés : {len(gardees)} gardés sur {len(sel.idees)}")
    if not gardees:
        print("Aucun lien vérifiable cette fois : on garde les idées d'avant.")
        return
    ecrire({"par_ia": True, "mon_style": sel.style, "suggestions": gardees[:EN_TOUT],
            "empreinte": empreinte, "styliste_le": quand})
    print(f"Terminé ✅ {len(gardees[:EN_TOUT])} idées de la styliste")


if __name__ == "__main__":
    main()
