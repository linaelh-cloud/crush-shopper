"""
Crush Shopper – le robot « Pour toi » (suggestions).

Pour chaque article de ma wishlist, il va sur sa page et récolte les articles proches
que la boutique propose (« vous aimerez aussi », même gamme, autres couleurs…).
Puis son « cerveau » regarde ce que j'aime (marques, catégories, couleurs, budget)
et donne une note à chaque suggestion. Les meilleures vont dans docs/suggestions.json,
avec la raison (« parce que tu aimes les tons vert olive »).

Deux façons de récolter :
  1. Boutique Shopify (Alo Yoga, Gymshark, Rhode…) : elle a un guichet de recommandations
     tout prêt (/recommendations/products.json) qui donne nom, prix, photo et lien.
  2. Autres boutiques (MAC…) : on lit les « cartes produit » affichées sur la page
     (un lien vers un produit + une photo + un nom + un prix).

On ne contourne jamais une protection : si un site refuse le robot, on passe au suivant.
"""
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

import sites

DOSSIER = Path(__file__).parent
WISHLIST = DOSSIER / "wishlist.json"
ETAT = DOSSIER / "etat.json"
SORTIE = DOSSIER / "docs" / "suggestions.json"
PAR_ARTICLE = 12      # suggestions gardées au maximum par article
EN_TOUT = 40          # suggestions affichées au maximum

# Familles de couleurs : pour comprendre que « Bamboo Green » et « Olive Tree » se ressemblent.
COULEURS = {
    "vert": ["vert", "green", "olive", "sage", "kaki", "khaki", "bamboo", "mint", "forest", "moss", "pistache"],
    "noir": ["noir", "black", "onyx", "ebony"],
    "nude": ["nude", "beige", "sand", "sable", "taupe", "camel", "fleshpot", "modesty", "tan", "latte", "creme", "cream"],
    "rose": ["rose", "pink", "blush", "rosy", "fuchsia", "berry"],
    "rouge": ["rouge", "red", "ruby", "cherry", "bordeaux", "burgundy", "wine"],
    "bleu": ["bleu", "blue", "navy", "marine", "denim", "sky"],
    "blanc": ["blanc", "white", "ivory", "ivoire", "ecru", "off-white"],
    "marron": ["marron", "brown", "chocolate", "cocoa", "espresso", "caramel", "toffee", "mocha"],
    "gris": ["gris", "grey", "gray", "titanium", "anthracite", "charcoal", "heather", "silver"],
}
PRIX = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)\s?€")


def sans_accents(t):
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def couleurs_de(texte):
    t = sans_accents(texte)
    return {fam for fam, mots in COULEURS.items() if any(re.search(r"\b" + m + r"\b", t) for m in mots)}


def base(url):
    """L'adresse d'un produit sans ses « ?… » : pour reconnaître le même produit."""
    u = urlparse(url)
    return f"{u.netloc}{u.path}".rstrip("/").lower()


def lire_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


# ---------------------------------------------------------------- récolte
def recolter_shopify(page, url):
    """Boutique Shopify : on demande ses recommandations au guichet prévu pour ça."""
    chemin = urlparse(url).path
    if "/products/" not in chemin:
        return []
    langue = chemin.split("/products/")[0]
    try:
        r = page.evaluate("""async ([chemin, langue]) => {
          const p = await fetch(chemin + '.js'); if (!p.ok) return null;
          const id = (await p.json()).id;
          const r = await fetch(langue + '/recommendations/products.json?product_id=' + id + '&limit=12');
          return r.ok ? await r.json() : null;
        }""", [chemin, langue])
    except Exception:
        return []
    trouves = []
    for x in (r or {}).get("products", []):
        img = x.get("featured_image") or ""
        trouves.append({
            "nom": x.get("title", ""),
            "lien": urljoin(url, (x.get("url") or "").split("?")[0]),
            "photo": ("https:" + img) if img.startswith("//") else img,
            "prix": sites._joli_prix(x["price"] / 100) if isinstance(x.get("price"), (int, float)) else "",
            "marque": x.get("vendor", ""),
        })
    return trouves


def recolter_liens(page, url, combien=8):
    """Autres boutiques : on repère les liens vers d'autres produits du même site,
    puis on ouvre chaque fiche pour lire le VRAI nom, la photo, le prix et le stock."""
    hote = urlparse(url).netloc
    liens = page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href]')]
        .map(a => a.href.split('#')[0].split('?')[0])
        .filter(h => /\/(product|products|p)\/|\.html$/i.test(h)))]""")
    candidats = [h for h in liens if urlparse(h).netloc == hote and base(h) != base(url)][:combien]
    trouves = []
    for h in candidats:
        try:
            r = page.goto(h, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2500)
            if sites.page_bloquee(page.title(), r.status if r else 0):
                continue
            html = page.content()
            infos = sites.lire(h, html)
            if infos.get("nom") and infos.get("photo"):
                trouves.append({"nom": infos["nom"], "lien": h, "photo": infos["photo"],
                                "prix": infos.get("prix", ""), "marque": infos.get("marque", ""),
                                "statut": infos.get("statut", "")})
        except Exception:
            continue
    return trouves


# ---------------------------------------------------------------- comprendre un article
# Mots du nom de l'article -> type de produit en français (pour chercher chez d'autres marques)
TYPES = [
    (r"\b(lipstick|rouge a levres|rouge-a-levres)\b", "rouge à lèvres", "rouge à lèvres"),
    (r"\b(lip ?gloss|gloss|lip oil|huile a levres)\b", "gloss", "gloss"),
    (r"\b(lip ?liner|crayon a levres)\b", "crayon à lèvres", "crayon"),
    (r"\b(mascara)\b", "mascara", "mascara"),
    (r"\b(foundation|fond de teint)\b", "fond de teint", "fond de teint"),
    (r"\b(blush)\b", "blush", "blush"),
    (r"\b(perfume|parfum|eau de parfum|eau de toilette|fragrance)\b", "parfum", "parfum"),
    (r"\b(jacket|veste|blouson|windbreaker|coupe-vent)\b", "veste", "veste"),
    (r"\b(coat|manteau|trench|parka)\b", "manteau", "manteau"),
    (r"\b(dress|robe)\b", "robe", "robe"),
    (r"\b(skirt|jupe)\b", "jupe", "jupe"),
    (r"\b(jogger|jogging|sweatpant)\b", "jogging", "jogging"),
    (r"\b(pant|pants|pantalon|trouser)\b", "pantalon", "pantalon"),
    (r"\b(legging)\b", "legging", "legging"),
    (r"\b(jean|jeans)\b", "jean", "jean"),
    (r"\b(short|shorts)\b", "short", "short"),
    (r"\b(hoodie|sweat|sweatshirt)\b", "sweat", "sweat"),
    (r"\b(sweater|pull|cardigan|knit)\b", "pull", "pull"),
    (r"\b(shirt|chemise|blouse)\b", "chemise", "chemise"),
    (r"\b(t-shirt|tee|top|bra|brassiere)\b", "top", "top"),
    (r"\b(bag|sac|tote|pochette)\b", "sac", "sac"),
    (r"\b(sneaker|sneakers|basket|baskets|runner|trainer)\b", "baskets", "basket"),
    (r"\b(boot|boots|bottes|bottine)\b", "bottes", "botte"),
]
GENRE = {"rouge à lèvres": "ton", "gloss": "ton", "crayon à lèvres": "ton", "mascara": "ton", "fond de teint": "ton",
         "blush": "ton", "parfum": "ton", "veste": "ta", "manteau": "ton", "robe": "ta", "jupe": "ta", "jogging": "ton",
         "pantalon": "ton", "legging": "ton", "jean": "ton", "short": "ton", "sweat": "ton", "pull": "ton",
         "chemise": "ta", "top": "ton", "sac": "ton", "baskets": "tes", "bottes": "tes"}
COULEUR_FR = {"vert": "kaki", "noir": "noir", "nude": "nude", "rose": "rose", "rouge": "rouge",
              "bleu": "bleu", "blanc": "blanc", "marron": "marron", "gris": "gris"}
FINITIONS = ["satin", "mat", "matte", "brillant", "velours", "soyeux"]


def comprendre(nom, option, url):
    """« Performance On-The-Go Jacket - Olive Tree » -> (« veste kaki », « veste »)."""
    texte = sans_accents(" ".join([nom, option, url.replace("-", " ")]))
    typ, mot = "", ""
    for motif, fr, m in TYPES:
        if re.search(motif, texte):
            typ, mot = fr, m
            break
    cs = couleurs_de(texte)
    couleur = COULEUR_FR[sorted(cs)[0]] if cs else ""
    fin = next((f for f in FINITIONS if f in texte), "")
    if fin == "matte":
        fin = "mat"
    requete = " ".join(x for x in [typ, couleur, fin if typ == "rouge à lèvres" else ""] if x).strip()
    return requete, mot


# ---------------------------------------------------------------- les grands magasins multi-marques
BADGES = re.compile(r"\b(Promo|Sponsoris[ée]|Luxe|Nouveau|Meilleure vente|Meilleure note|Combinaisons parfaites|"
                    r"Mix & match|heart_outlined|Durable|Exclusivit[ée])\b", re.I)


PAS_POUR_MOI = re.compile(r"\\b(homme|men|kids|teen|junior|enfant|b[ée]b[ée]|boys|girls)\\b", re.I)


def recolter_zalando(page, requete):
    """Cherche sur Zalando (des centaines de marques) et lit les cartes produit des résultats."""
    url = "https://www.zalando.fr/catalogue/?q=" + requete.replace(" ", "+")
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(4000)
        if sites.page_bloquee(page.title(), r.status if r else 0):
            return []
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1500)
        cartes = page.evaluate("""() => [...document.querySelectorAll('article')].map(a => {
            const l = a.querySelector('a[href]'), img = a.querySelector('img');
            return { lien: l && l.href.split('?')[0], photo: img && (img.currentSrc || img.src),
                     texte: (a.innerText || '').replace(/\\s+/g, ' ').trim(),
                     titre: ((a.querySelector('h3') || {}).innerText || '').split('\\n').map(x => x.trim()).filter(Boolean) };
          }).filter(x => x.lien && /\\.html$/.test(x.lien) && x.photo && !x.photo.startsWith('data:'))""")
    except Exception:
        return []
    trouves = []
    for c in cartes:
        t = BADGES.sub(" ", c["texte"])
        t = re.sub(r"\s+", " ", t).strip()
        prix = PRIX.findall(t)
        if not prix:
            continue
        if len(c["titre"]) < 2:
            continue
        marque, nom = c["titre"][0], " ".join(c["titre"][1:])
        if PAS_POUR_MOI.search(marque + " " + nom):
            continue
        promo = re.search(r"-\s?(\d{1,2})\s?%", t)
        trouves.append({
            "nom": nom[:110], "lien": c["lien"], "photo": c["photo"],
            "prix": sites._joli_prix(prix[0]),
            "prix_avant": sites._joli_prix(prix[1]) if len(prix) > 1 and "médian" in t else "",
            "promo": f"-{promo.group(1)} %" if promo else "",
            "marque": marque,
            "boutique": "Zalando",
        })
    return trouves


# ---------------------------------------------------------------- le « cerveau » à règles
def mon_style(articles, etat):
    """Ce que j'aime, d'après ma wishlist : marques, catégories, couleurs, budget."""
    marques, cats, couleurs, prix = {}, {}, {}, []
    for a in articles:
        e = etat.get(a["url"], {})
        texte = " ".join([a.get("nom", ""), e.get("nom", ""), a.get("option", ""), a["url"]])
        for c in couleurs_de(texte):
            couleurs[c] = couleurs.get(c, 0) + 1
        m = (e.get("marque") or "").lower()
        if m:
            marques[m] = marques.get(m, 0) + 1
        cat = a.get("categorie") or ("Beauté" if "maccosmetics" in a["url"] else "Mode")
        cats[cat] = cats.get(cat, 0) + 1
        p = re.sub(r"[^\d,]", "", e.get("prix", "")).replace(",", ".")
        if p:
            try:
                prix.append(float(p))
            except ValueError:
                pass
    return {"marques": marques, "categories": cats, "couleurs": couleurs,
            "prix_median": median(prix) if prix else None}


def meme_produit(nom, noms_wishlist):
    """« MACXIMAL SLEEK SATIN LIPSTICK » = mon « M·A·Cximal Sleek Satin Lipstick » : déjà dans ma wishlist."""
    a = re.sub(r"[^a-z]", "", sans_accents(nom).split(" - ")[0])
    return any(len(b) > 8 and (b in a or a in b) for b in (re.sub(r"[^a-z]", "", sans_accents(n)) for n in noms_wishlist))


def valeur(prix):
    p = re.sub(r"[^\d,]", "", prix or "").replace(",", ".")
    try:
        return float(p)
    except ValueError:
        return None


def noter(sugg, source, rang):
    """Une note pour chaque produit trouvé, et sa raison."""
    note, raisons = 0.0, []
    texte = sans_accents(sugg["nom"])
    if source["mot"] and sans_accents(source["mot"]) in texte:
        note += 3
    elif source["mot"]:
        return -1, ""      # pas le bon type de produit (ex. une montre quand je cherche un rouge à lèvres)
    communes = couleurs_de(sugg["nom"] + " " + sugg["lien"]) & source["couleurs"]
    if communes:
        note += 3
        raisons.append("même couleur")
    p, ref = valeur(sugg.get("prix")), source["prix"]
    if p and ref:
        if p <= ref * 0.8:
            note += 2
            raisons.append("moins cher")
        elif p <= ref * 1.3:
            note += 1
    if sugg.get("promo"):
        note += 1
        raisons.append(f"en promo {sugg['promo']}")
    note += max(0, 1.5 - rang * 0.1)
    raison = source["comme"] + (" · " + " · ".join(raisons[:2]) if raisons else "")
    return round(note, 2), raison


def main():
    import ia
    articles = lire_json(WISHLIST, {"articles": []})["articles"]
    etat = lire_json(ETAT, {})
    style = mon_style(articles, etat)
    deja = {base(a["url"]) for a in articles}
    sources = {}
    for a in articles:
        e = etat.get(a["url"], {})
        nom = e.get("nom") or a.get("nom") or "ton article"
        requete, mot = comprendre(nom, a.get("option", ""), a["url"])
        typ = next((t for t in GENRE if requete.startswith(t)), "")
        marque_courte = (e.get("marque") or "").replace(" Cosmetics", "").replace(" Yoga", "")
        comme = f"Comme {GENRE[typ]} {typ} {marque_courte}".strip() if typ else f"Comme ton {nom[:30]}"
        if requete and a.get("categorie", "Beauté" if "maccosmetics" in a["url"] else "Mode") != "Beauté":
            requete += " femme"
        sources[a["url"]] = {"url": a["url"], "nom": nom, "nom_court": nom[:38], "option": a.get("option", ""),
                             "marque": e.get("marque", ""), "prix_texte": e.get("prix", ""), "prix": valeur(e.get("prix")),
                             "categorie": a.get("categorie") or ("Beauté" if "maccosmetics" in a["url"] else "Mode"),
                             "couleurs": couleurs_de(" ".join([nom, a.get("option", ""), a["url"]])),
                             "requetes": [requete] if requete else [], "mot": mot,
                             "comme": comme}
    # L'IA (si la clé est rangée dans GitHub) écrit mon style et invente de meilleures recherches.
    texte_style, par_ia = "", False
    if ia.active():
        try:
            profil = ia.profil_et_recherches([{"url": s["url"], "nom": s["nom"], "option": s["option"], "marque": s["marque"],
                                               "prix": s["prix_texte"], "categorie": s["categorie"]} for s in sources.values()])
            if profil:
                texte_style, par_ia = profil.style, True
                for r in profil.recherches:
                    if r.pour_url in sources:
                        s = sources[r.pour_url]
                        s["requetes"] = list(dict.fromkeys(s["requetes"] + [r.requete]))[:4]
                        s["mot"] = s["mot"] or r.type_produit
                print("🤖 IA : style =", texte_style)
        except Exception as ex:
            print(f"🤖 IA indisponible ({ex.__class__.__name__}) : cerveau à règles")
    candidats = {}
    with sync_playwright() as pw:
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for s in sources.values():
            n = 0
            for q in s["requetes"]:
                for rang, sugg in enumerate(recolter_zalando(page, q)):
                    b = base(sugg["lien"])
                    if b in deja:
                        continue
                    if meme_produit(sugg["nom"], [x["nom"] for x in sources.values()]):
                        continue
                    note, raison = noter(sugg, s, rang)
                    if note < 0:
                        continue
                    sugg.update(note=note, raison=raison, pour=s["url"], pour_nom=s["nom_court"],
                                categorie=s["categorie"], recherche=q, comme=s["comme"])
                    if b not in candidats or candidats[b]["note"] < note:
                        candidats[b] = sugg
                        n += 1
            print(f"✅ {s['nom_court']} : recherches {s['requetes']} → {n} produits")
        navigateur.close()
    # Tri : la meilleure note d'abord, en mélangeant les marques (2 par marque au maximum)
    tries = sorted(candidats.values(), key=lambda x: -x["note"])
    par_marque, choisis = {}, []
    for c in tries:
        m = (c.get("marque") or "").lower()
        if par_marque.get(m, 0) >= 2:
            continue
        par_marque[m] = par_marque.get(m, 0) + 1
        choisis.append(c)
    choisis = choisis[:60]
    # L'IA choisit ceux qui me ressemblent le plus et écrit une raison perso.
    if par_ia and choisis:
        try:
            raisons = ia.choisir(texte_style, choisis)
            if raisons:
                choisis = [dict(choisis[i], raison=r, choisi_par_ia=True) for i, r in raisons.items() if 0 <= i < len(choisis)]
        except Exception as ex:
            print(f"🤖 IA indisponible pour le choix ({ex.__class__.__name__})")
    choisis = choisis[:EN_TOUT]
    if not choisis:
        # Rien trouvé (site qui refuse le robot, panne…) : on garde les anciennes suggestions.
        print("Aucun produit trouvé cette fois : on garde les suggestions d'avant.")
        return
    if not texte_style:
        tons = ", ".join(sorted(style["couleurs"], key=lambda c: -style["couleurs"][c])) or "variés"
        texte_style = f"Tes tons préférés : {tons}" + (f" · budget autour de {style['prix_median']:.0f} €" if style["prix_median"] else "")
    SORTIE.write_text(json.dumps({
        "mise_a_jour": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "par_ia": par_ia, "mon_style": texte_style, "suggestions": choisis,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Terminé ✅ {len(choisis)} suggestions ({'choisies par l IA' if par_ia else 'cerveau à règles'})")


if __name__ == "__main__":
    main()
