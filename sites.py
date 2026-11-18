"""
Les « lecteurs » de sites : pour chaque site appris, une fonction qui lit
le nom, la photo et le stock d'un article sur sa page.

Le robot sait lire :
  - MAC Cosmetics, couleur par couleur (lecteur « appris ») ;
  - (presque) tous les autres sites, grâce au LECTEUR UNIVERSEL : il lit la petite
    « fiche produit standard » (schema.org, au format JSON-LD) que beaucoup de boutiques
    cachent dans leurs pages pour Google : nom, photo, et « en stock / en rupture ».
"""
import html
import re
from urllib.parse import parse_qs, unquote, urlparse

# Statuts possibles d'un article
DISPO = "dispo"
RUPTURE = "rupture"
INCONNU = "inconnu"      # le robot n'a pas réussi à lire la page
BLOQUE = "bloque"        # le site refuse le robot (anti-bot / CAPTCHA)
NON_APPRIS = "non_appris"  # site que le robot ne sait pas encore lire


def site_de(url):
    """Le nom du site à afficher : « MAC Cosmetics », sinon le nom de domaine (ex. « rouje.com »)."""
    hote = urlparse(url).netloc.lower()
    if "maccosmetics." in hote:
        return "MAC Cosmetics"
    return hote[4:] if hote.startswith("www.") else (hote or None)


def lecteur_de(url):
    """Quel lecteur utiliser : « mac » (appris) ou « universel » (fiche standard)."""
    return "mac" if "maccosmetics." in urlparse(url).netloc.lower() else "universel"


def page_bloquee(titre, code=200):
    """Reconnaît une page anti-robot (on ne contourne jamais : on s'arrête)."""
    t = (titre or "").lower()
    return code in (403, 429) or any(m in t for m in
        ("access denied", "un instant", "just a moment", "attention required", "captcha", "are you a robot"))


def teinte_dans_url(url):
    valeur = parse_qs(urlparse(url).query).get("shade", [""])[0]
    return unquote(valeur).replace("_", " ").strip()


def lire_mac(page_html, teinte=""):
    """Lit une page produit MAC. Retourne un dict : nom, teinte, photo, statut."""
    h = html.unescape(page_html)
    infos = {"nom": "", "teinte": teinte, "photo": "", "statut": INCONNU}

    titre = re.search(r"<title>([^<|]+)", h)
    if titre:
        infos["nom"] = titre.group(1).strip()

    if teinte:
        # Les données cachées de la page donnent, pour chaque teinte,
        # son « inventory_status » (active = disponible).
        statut = None
        for m in re.finditer(r'"inventory_status":"([a-z_]+)"', h):
            avant = h.rfind('"shades":[{', 0, m.start())
            apres = h.find('"shades":[{', m.start())
            candidats = [x for x in (avant, apres) if x != -1]
            if not candidats:
                continue
            pos = min(candidats, key=lambda x: abs(x - m.start()))
            t = re.match(r'"shades":\[\{[^\]]*?"title":"([^"]+)"', h[pos:pos + 600])
            if t and t.group(1).lower() == teinte.lower():
                statut = m.group(1)
                break
        if statut:
            infos["statut"] = DISPO if statut == "active" else RUPTURE
        photo = re.search(r'"alt":"[^"]*, ' + re.escape(teinte) + r', Product Shot","src":"([^"]+)"', h, re.I)
        if photo:
            infos["photo"] = photo.group(1)

    if infos["statut"] == INCONNU:
        # Sinon : l'attribut du bloc produit affiché.
        m = re.search(r'class="product-details"[^>]*data-product-is-shoppable-value="(true|false)"', page_html)
        if m:
            infos["statut"] = DISPO if m.group(1) == "true" else RUPTURE

    if not infos["photo"]:
        og = re.search(r'<meta property="og:image" content="([^"]+)"', page_html)
        if og:
            infos["photo"] = og.group(1)
    return infos


# ------------------------------------------------------------------ lecteur universel
EN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly")


def _fiches_produit(page_html):
    """Trouve les « fiches produit standard » (JSON-LD de type Product) cachées dans la page."""
    import json
    fiches = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            donnees = json.loads(html.unescape(m.group(1).strip()))
        except Exception:
            continue
        pile = [donnees]
        while pile:
            x = pile.pop()
            if isinstance(x, list):
                pile += x
                continue
            if not isinstance(x, dict):
                continue
            if "@graph" in x:
                pile += x["@graph"] if isinstance(x["@graph"], list) else [x["@graph"]]
            types = x.get("@type") if isinstance(x.get("@type"), list) else [x.get("@type")]
            if "Product" in types or "ProductGroup" in types:
                fiches.append(x)
    return fiches


def _offres(fiche):
    """Toutes les offres (une par taille/couleur, souvent) avec leur étiquette et leur stock."""
    offres = fiche.get("offers") or []
    offres = offres if isinstance(offres, list) else [offres]
    resultat = []
    for o in offres:
        if isinstance(o, dict) and o.get("@type") == "AggregateOffer" and o.get("offers"):
            offres += o["offers"] if isinstance(o["offers"], list) else [o["offers"]]
            continue
        if isinstance(o, dict):
            etiquette = " ".join(str(o.get(k, "")) for k in ("name", "sku", "url", "size", "color"))
            resultat.append((etiquette, str(o.get("availability", "")).lower()))
    for v in fiche.get("hasVariant") or []:  # variantes (ProductGroup)
        if isinstance(v, dict):
            sous = v.get("offers") or []
            sous = sous if isinstance(sous, list) else [sous]
            for o in sous:
                etiquette = " ".join(str(v.get(k, "")) for k in ("name", "sku", "size", "color"))
                resultat.append((etiquette, str(o.get("availability", "")).lower()))
    return resultat


def _mots_option(option):
    """« Taille 38 » -> ["38"] ; « Teinte Modesty » -> ["modesty"]."""
    mots = re.sub(r"(?i)\b(taille|teinte|couleur|pointure|size|color)\b", " ", option or "")
    return [m.lower() for m in re.split(r"[\s/,;–-]+", mots) if m.strip()]


def _correspond(etiquette, mots):
    morceaux = set(re.split(r"[\s/,;:._|()–-]+", etiquette.lower()))
    return all(m in morceaux for m in mots)


def _variante_du_lien(url):
    """Le numéro de variante dans le lien (ex. « ?variant=50661911396711 ») = couleur + taille exactes."""
    m = re.search(r"[?&](?:variant|variantId|sku|skuId)=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _couleur_du_nom(nom):
    """« Veste Performance - Olive Tree » -> « olive tree » (la couleur après le tiret)."""
    morceaux = re.split(r"\s[-–|]\s", nom or "")
    return morceaux[-1].strip().lower() if len(morceaux) > 1 else ""


def lire_universel(page_html, option="", url=""):
    """Lit nom, photo et stock grâce à la fiche standard.
    Priorité : 1) la variante exacte du lien (couleur + taille), 2) ma taille/teinte
    (en gardant la couleur de l'article), 3) l'article entier."""
    infos = {"nom": "", "teinte": option, "photo": "", "statut": INCONNU, "precision": ""}
    fiches = _fiches_produit(page_html)
    og_titre = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page_html)
    og_image = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page_html)
    if og_titre:
        infos["nom"] = html.unescape(og_titre.group(1)).strip()
    if og_image:
        infos["photo"] = og_image.group(1)
    if not fiches:
        infos["precision"] = "pas de fiche standard sur cette page"
        return infos

    fiche = fiches[0]
    infos["nom"] = html.unescape(str(fiche.get("name") or infos["nom"])).strip()
    image = fiche.get("image")
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    if isinstance(image, str) and image:
        infos["photo"] = "https:" + image if image.startswith("//") else image

    offres = [o for f in fiches for o in _offres(f)]
    if not offres:
        infos["precision"] = "la fiche ne dit pas si c'est en stock"
        return infos
    mots = _mots_option(option)
    variante = _variante_du_lien(url)
    couleur = _couleur_du_nom(infos["nom"])
    choisies = [o for o in offres if variante and variante in o[0]]
    if choisies:
        infos["precision"] = "variante exacte du lien (couleur + taille)"
    else:
        choisies = [o for o in offres if mots and _correspond(o[0], mots)]
        # Plusieurs couleurs dans la fiche ? On garde celle de l'article.
        meme_couleur = [o for o in choisies if couleur and couleur in o[0].lower()]
        if meme_couleur:
            choisies = meme_couleur
    if choisies and not infos["precision"]:
        infos["precision"] = f"stock lu pour « {option} »" + (f", couleur {couleur.title()}" if couleur and any(couleur in o[0].lower() for o in choisies) else "")
    if not choisies:
        choisies = offres
        infos["precision"] = ("taille/teinte introuvable sur la fiche : stock de l'article entier"
                              if mots else "stock de l'article entier")
    disponible = any(any(m in disp for m in EN_STOCK) for _, disp in choisies)
    infos["statut"] = DISPO if disponible else RUPTURE
    return infos


def _joli_prix(montant, devise="EUR"):
    """« 12500.00 » + EUR -> « 12 500 € » ; « 29.9 » -> « 29,90 € »."""
    try:
        x = float(str(montant).replace(",", "."))
    except ValueError:
        return ""
    if x <= 0:
        return ""
    texte = f"{x:,.2f}".replace(",", "\u202f").replace(".", ",")
    if texte.endswith(",00"):
        texte = texte[:-3]
    symbole = {"EUR": "€", "USD": "$", "GBP": "£"}.get((devise or "EUR").upper(), devise or "€")
    return f"{texte} {symbole}"


def marque_et_prix(page_html):
    """La marque et le prix, pour l'affichage de la wishlist (fiche standard, sinon balises meta)."""
    marque, prix = "", ""
    for fiche in _fiches_produit(page_html):
        b = fiche.get("brand")
        if isinstance(b, list) and b:
            b = b[0]
        if isinstance(b, dict):
            b = b.get("name", "")
        if isinstance(b, str) and b.strip() and not marque:
            marque = html.unescape(b).strip()
        offres = fiche.get("offers") or []
        offres = offres if isinstance(offres, list) else [offres]
        for o in offres:
            if isinstance(o, dict) and not prix:
                prix = _joli_prix(o.get("price") or o.get("lowPrice") or "", o.get("priceCurrency") or "EUR")
    if not prix:
        m = re.search(r'<meta[^>]+property="(?:product|og):price:amount"[^>]+content="([^"]+)"', page_html)
        d = re.search(r'<meta[^>]+property="(?:product|og):price:currency"[^>]+content="([^"]+)"', page_html)
        if m:
            prix = _joli_prix(m.group(1), d.group(1) if d else "EUR")
    if not marque:
        m = re.search(r'<meta[^>]+property="(?:product:brand|og:brand)"[^>]+content="([^"]+)"', page_html)
        if m:
            marque = html.unescape(m.group(1)).strip()
    return marque, prix


# Les prix en euros : l'ordinateur de GitHub est aux États-Unis, alors certains sites (ex. Alo Yoga)
# lui montrent des dollars. On convertit au taux du jour de la Banque centrale européenne,
# avec « ≈ » devant, pour dire que c'est une conversion (le vrai prix en France peut être différent).
_taux = {}


def en_euros(prix):
    m = re.match(r"^\s*([\d\u202f.,]+)\s*([$£])\s*$", prix or "")
    if not m:
        return prix
    devise = {"$": "USD", "£": "GBP"}[m.group(2)]
    if devise not in _taux:
        try:
            import json
            import urllib.request
            r = urllib.request.urlopen(f"https://api.frankfurter.dev/v1/latest?base={devise}&symbols=EUR", timeout=15)
            _taux[devise] = json.loads(r.read())["rates"]["EUR"]
        except Exception:
            _taux[devise] = None
    if not _taux[devise]:
        return prix
    montant = float(m.group(1).replace("\u202f", "").replace(",", "."))
    return "≈ " + _joli_prix(round(montant * _taux[devise]))


def lire(url, page_html, option="", teinte=""):
    """Choisit le bon lecteur selon le site."""
    if lecteur_de(url) == "mac":
        infos = lire_mac(page_html, teinte)
        infos.setdefault("precision", f"teinte « {teinte} »" if teinte else "")
    else:
        infos = lire_universel(page_html, option, url)
    marque, prix = marque_et_prix(page_html)
    if lecteur_de(url) == "mac":
        marque = "MAC Cosmetics"
        if not prix:  # MAC range son prix dans ses données cachées
            m = re.search(r'"price"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)', html.unescape(page_html))
            prix = _joli_prix(m.group(1)) if m else ""
    infos["marque"], infos["prix"] = marque, en_euros(prix)
    return infos
