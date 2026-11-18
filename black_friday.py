"""
Crush Shopper – le robot « Black Friday ».

Il ouvre la page (Black Friday, ou d'accueil) de chaque marque de docs/black-friday.json,
lit le texte affiché, et note ce qu'il voit VRAIMENT sur le site :
  - les réductions annoncées (« jusqu'à -50 % », « -30 % ») ;
  - les codes promo affichés (« code BLACK20 ») ;
  - si la page parle du Black Friday ;
  - si le site bloque les robots (dans ce cas on s'arrête : on ne contourne jamais).
Le résultat est rangé dans « vu_sur_le_site » de chaque marque, avec la date.
On ne change jamais le texte écrit à la main (« offre ») : on ajoute juste ce que le robot a vu.

Lancer :  python black_friday.py            (toutes les marques)
          python black_friday.py Sephora Etam  (seulement certaines marques)
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import sites

FICHIER = Path(__file__).parent / "docs" / "black-friday.json"

# « jusqu'à -50 % », « -30% », « - 25 % »
JUSQUA = re.compile(r"jusqu['’]?\s*(?:à|a)\s*-?\s?(\d{1,2})\s?%", re.I)
POURCENT = re.compile(r"(?<![\d,.])-\s?(\d{1,2})\s?%")
# « code BLACK20 », « avec le code : NOTINO »
CODE = re.compile(r"\bcode(?:\s+promo)?\s*[:«\"“]?\s*([A-Za-z][A-Za-z0-9]{3,14})\b", re.I)
MOTS_BF = re.compile(r"black\s*(?:friday|week|month|november)|cyber\s*(?:monday|week)", re.I)
# Mots qui ne sont pas des codes (en majuscules dans la page)
PAS_DES_CODES = {"promo", "postal", "code", "black", "friday", "offre", "valable", "client", "acces", "accès",
                 "pour", "dans", "avec", "votre", "vous", "sera", "sans", "dans", "suivant", "unique", "cadeau",
                 "parrainage", "secret", "panier", "commande", "réduction", "reduction", "applique", "appliqué"}


def lire_promos(texte):
    """Trouve les réductions et les codes dans le texte d'une page."""
    jusqua = sorted({int(x) for x in JUSQUA.findall(texte) if 5 <= int(x) <= 90}, reverse=True)
    tous = [int(x) for x in POURCENT.findall(texte) if 5 <= int(x) <= 90]
    # Les % les plus fréquents = ceux des bandeaux (pas un seul produit très soldé)
    frequents = sorted(set(tous), key=lambda p: (-tous.count(p), -p))[:4]
    codes = sorted({c for c in CODE.findall(texte) if c.lower() not in PAS_DES_CODES})[:4]
    return {
        "jusqua": f"-{jusqua[0]} %" if jusqua else "",
        "reductions": [f"-{p} %" for p in frequents],
        "codes": codes,
        "parle_du_black_friday": bool(MOTS_BF.search(texte)),
    }


# ---------------------------------------------------------------- les sites d'annonces (dates)
MOIS = {"octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
UNE_DATE = r"(?:(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?(\d{1,2})(?:er)?\s+(octobre|novembre|d[ée]cembre)"
PERIODE = re.compile(r"(?:du|dès le|à partir du|a partir du)\s+" + UNE_DATE + r"(?:\s+(?:2026\s+)?au\s+" + UNE_DATE + r")?", re.I)


def date_2026(jour_nom, jour, mois):
    """La date en 2026, SEULEMENT si le jour de la semaine écrit correspond bien à 2026.
    (Les sites d'annonces mélangent 2025 et 2026 : « mercredi 19 novembre », c'est 2025.)"""
    from datetime import date
    try:
        d = date(2026, MOIS[mois.lower()], int(jour))
    except (ValueError, KeyError):
        return None
    if not jour_nom or JOURS[d.weekday()] != jour_nom.lower():
        return None
    return d.isoformat()


def dates_annoncees(texte):
    """Cherche « du jeudi 26 novembre au lundi 30 novembre » et garde les dates sûres pour 2026."""
    for m in PERIODE.finditer(texte):
        debut = date_2026(m.group(1), m.group(2), m.group(3))
        fin = date_2026(m.group(4), m.group(5), m.group(6)) if m.group(5) else ""
        if debut and fin is not None:
            return {"debut": debut, "fin": fin or "", "phrase": " ".join(m.group(0).split())}
    return None


def etat_du_jour(m, aujourd_hui=None):
    """« en cours », « à venir » ou « terminé », d'après ce que le robot a vu et les dates."""
    from datetime import date
    jour = (aujourd_hui or date.today()).isoformat()
    vu = m.get("vu_sur_le_site", {})
    debut = m.get("debut") or (m.get("dates_annonce") or {}).get("debut", "")
    fin = m.get("fin") or (m.get("dates_annonce") or {}).get("fin", "")
    if fin and jour > fin:
        return "terminé"
    if vu.get("parle_du_black_friday") and (vu.get("jusqua") or vu.get("reductions")):
        return "en cours"
    if debut and jour >= debut and (not fin or jour <= fin) and m.get("statut") == "officiel":
        return "en cours"
    return "à venir"


def maintenant():
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def lire_dates_annonce(page, m):
    """Va sur la page de la marque d'un site d'annonces, et lit ses dates 2026 (si elles sont sûres)."""
    import unicodedata
    nom = unicodedata.normalize("NFKD", m["marque"]).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", nom).strip("-")
    url = f"https://quelles-dates.fr/black-friday-{slug}/"
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)
        if not r or r.status != 200 or sites.page_bloquee(page.title(), r.status):
            return None
        trouve = dates_annoncees(page.inner_text("body")[:40_000])
        if trouve:
            trouve["source"] = url
            print(f"   📅 dates annoncées : {trouve['phrase']}")
        return trouve
    except Exception:
        return None


seulement_dates_off = False


def main(seulement=()):
    donnees = json.loads(FICHIER.read_text(encoding="utf-8"))
    marques = [m for m in donnees["marques"] if not seulement or m["marque"].lower() in seulement]
    print(f"Le robot va lire {len(marques)} page(s)…")
    with sync_playwright() as pw:
        # Fenêtre visible : beaucoup de boutiques refusent les navigateurs invisibles.
        navigateur = pw.chromium.launch(headless=False)
        page = navigateur.new_page(locale="fr-FR", viewport={"width": 1280, "height": 900})
        for m in marques:
            vu = {"le": maintenant()}
            try:
                reponse = page.goto(m["lien"], wait_until="domcontentloaded", timeout=35_000)
                page.wait_for_timeout(3500)
                code = reponse.status if reponse else 0
                if sites.page_bloquee(page.title(), code) or code >= 400:
                    vu["bloque"] = True
                    print(f"⛔ {m['marque']} : le site refuse le robot (code {code}), on n'insiste pas")
                else:
                    vu.update(lire_promos(page.inner_text("body")[:60_000]))
                    print(f"✅ {m['marque']} : jusqu'à {vu['jusqua'] or '?'} | vu : {vu['reductions']} | codes : {vu['codes']} "
                          f"| Black Friday : {'oui' if vu['parle_du_black_friday'] else 'non'}")
            except Exception as e:
                vu["erreur"] = e.__class__.__name__
                print(f"⚠️ {m['marque']} : page impossible à lire ({vu['erreur']})")
            m["vu_sur_le_site"] = vu
            # Les dates sur un site d'annonces (quelles-dates.fr a une page par enseigne)
            if not seulement_dates_off:
                m["dates_annonce"] = lire_dates_annonce(page, m) or m.get("dates_annonce")
            m["etat"] = etat_du_jour(m)
            # On enregistre après chaque marque : si le robot s'arrête, rien n'est perdu.
            FICHIER.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        navigateur.close()
    donnees["robot_passe_le"] = maintenant()
    FICHIER.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Terminé ✅")


if __name__ == "__main__":
    main({a.lower() for a in sys.argv[1:]})
