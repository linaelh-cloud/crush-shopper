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
    src = sources.get(idee.pour_url) or next(iter(sources.values()))
    v = {"nom": idee.nom + (f" - {idee.teinte_ou_couleur}" if idee.teinte_ou_couleur else ""),
         "lien": page.url.split("#")[0], "photo": infos["photo"], "prix": infos.get("prix") or idee.prix,
         "marque": idee.marque or infos.get("marque", ""), "boutique": urlparse(page.url).netloc.replace("www.", ""),
         "genre": idee.genre, "raison": idee.pourquoi, "choisi_par_ia": True,
         "pour": src["url"], "categorie": src["categorie"], "statut": infos.get("statut", "")}
    if infos.get("statut") == sites.RUPTURE:
        v["raison"] += " · en rupture pour l'instant"
    return v


def main():
    liste = lire_json(WISHLIST, {"articles": []})
    articles, marques_aimees = liste.get("articles", []), liste.get("marques_aimees", [])
    etat = lire_json(ETAT, {})
    avant = lire_json(SORTIE, {})

    # A. De nouvelles idées de la routine Crush Styliste ?
    routine = lire_json(STYLISTE, {})
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
