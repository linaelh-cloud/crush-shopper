"""
Les « lecteurs » de sites : pour chaque site appris, une fonction qui lit
le nom, la photo et le stock d'un article sur sa page.

Pour l'instant, le robot sait lire : MAC Cosmetics.
Pour apprendre un nouveau site, on ajoute une fonction ici.
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
    """Retourne le nom du site appris pour cette adresse, ou None."""
    hote = urlparse(url).netloc.lower()
    if "maccosmetics." in hote:
        return "MAC Cosmetics"
    return None


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
