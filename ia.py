"""
Crush Shopper – la styliste IA (Claude) pour l'onglet « Pour toi ».

Elle ne s'allume QUE si la clé ANTHROPIC_API_KEY est rangée dans le coffre-fort de GitHub.
Sans clé, le robot utilise son cerveau à règles (suggestions.py) : rien ne casse.

Ce qu'elle fait :
  1. elle lit ma wishlist (et mes marques préférées) ;
  2. elle cherche sur TOUT le web, avec l'outil officiel de recherche de Claude :
     les dupes dont on parle (TikTok, blogs, Reddit, magazines), les marques et teintes tendance,
     ce qu'aiment les filles qui achètent le même article ;
  3. elle rend une liste d'articles précis (marque, nom, teinte, lien de la fiche produit, pourquoi).
Le robot ouvre ensuite chaque lien pour vérifier photo, prix et stock.
"""
import os
from typing import List

from pydantic import BaseModel

MODELE = "claude-opus-5-5"
RECHERCHES_MAX = 15        # recherches web au maximum par passage (chacune coûte un peu)

OUTILS_WEB = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": RECHERCHES_MAX},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]


def active():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class Idee(BaseModel):
    pour_url: str          # le lien de l'article de ma wishlist qui a inspiré l'idée
    marque: str
    nom: str               # nom du produit
    teinte_ou_couleur: str # ex. « 32 Brownie », « kaki »
    lien: str              # lien DIRECT de la fiche produit (pas une page de recherche)
    prix: str              # ex. « 34 € » (vide si inconnu)
    genre: str             # « dupe », « tendance », « même style », « coup de cœur des filles »
    pourquoi: str          # phrase perso courte, en tutoiement


class Selection(BaseModel):
    style: str             # mon style en une phrase, en tutoiement
    idees: List[Idee]


def _client():
    import anthropic
    return anthropic.Anthropic()


def _consigne(articles, marques_aimees):
    lignes = "\n".join(
        f"- {a['url']} | {a['nom']} | {a.get('option', '')} | marque : {a.get('marque', '')} | prix : {a.get('prix', '')} "
        f"| catégorie : {a.get('categorie', '')}" for a in articles)
    aimees = ", ".join(marques_aimees) if marques_aimees else "(pas encore de liste : déduis-les de ma wishlist)"
    return (
        "Voici ma wishlist (lien | nom | taille ou teinte | marque | prix | catégorie) :\n" + lignes +
        f"\n\nMes marques préférées : {aimees}\n\n"
        "Fais une vraie recherche sur le web, comme une amie styliste qui suit TikTok, les blogs beauté et mode, "
        "Reddit et les magazines. Pour CHAQUE article de ma wishlist, trouve 5 à 8 produits précis qui me plairaient :\n"
        "- les DUPES et alternatives dont on parle (cherche par exemple « dupe » + nom + teinte, « TikTok », « Reddit ») ;\n"
        "- les marques et teintes TENDANCE en ce moment dans le même esprit ;\n"
        "- ce qu'aiment aussi les filles qui achètent cet article (avis, comparatifs, « si tu aimes X tu vas adorer ») ;\n"
        "- en priorité mes marques préférées, puis des marques du même niveau ou plus désirables, pas des marques bas de gamme.\n"
        "Pour un maquillage, la TEINTE doit être vraiment proche de la mienne (même famille de couleur et même sous-ton). "
        "Pour un vêtement, même type de pièce et couleur ou style proche.\n"
        "Varie les marques (pas plus de 2 idées par marque). Produits vendus en France (sites .fr ou livrant en France).\n"
        "IMPORTANT : donne seulement des liens de FICHES PRODUIT que tu as vraiment vus pendant ta recherche "
        "(jamais un lien inventé, jamais une page de recherche ou de catégorie).\n"
        "Termine par un résumé clair : mon style en une phrase, puis la liste des produits avec, pour chacun : "
        "le lien de l'article de ma wishlist concerné, la marque, le nom, la teinte ou couleur, le lien de la fiche, le prix, "
        "le genre (dupe / tendance / même style / coup de cœur des filles) et pourquoi il me plairait "
        "(15 mots maximum, en tutoiement, précis, ex. « Le dupe TikTok de ta Modesty, même nude rosé, en plus crémeux »)."
    )


def styliste_web(articles, marques_aimees=(), continuations_max=4):
    """Recherche sur le web puis mise en forme. Rend une Selection, ou None."""
    client = _client()
    messages = [{"role": "user", "content": _consigne(articles, marques_aimees)}]
    systeme = ("Tu es la styliste personnelle de l'appli Crush Shopper. Tu connais les tendances beauté et mode, "
               "les dupes TikTok et les avis des clientes. Tu es précise et tu ne recommandes que des produits réels.")
    # 1) La recherche sur le web (Claude cherche et lit les pages lui-même, sur les serveurs d'Anthropic)
    for _ in range(continuations_max + 1):
        with client.messages.stream(model=MODELE, max_tokens=32000, system=systeme,
                                    tools=OUTILS_WEB, messages=messages) as flux:
            reponse = flux.get_final_message()
        if reponse.stop_reason == "pause_turn":
            # La recherche est longue : on la laisse reprendre là où elle s'était arrêtée
            messages = [messages[0], {"role": "assistant", "content": reponse.content}]
            continue
        break
    if reponse.stop_reason == "refusal":
        return None
    compte_rendu = "\n".join(b.text for b in reponse.content if b.type == "text").strip()
    if not compte_rendu:
        return None
    # 2) La mise en forme : on transforme son compte rendu en liste bien rangée (formulaire)
    r = client.messages.parse(
        model=MODELE,
        max_tokens=16000,
        output_config={"effort": "low"},
        system="Tu recopies fidèlement un compte rendu de recherche dans le formulaire demandé, sans rien inventer.",
        messages=[{"role": "user", "content": (
            "Compte rendu de la styliste :\n\n" + compte_rendu +
            "\n\nRecopie mon style et chaque produit proposé. Garde seulement les produits qui ont un lien de fiche produit "
            "(http…). « pour_url » doit être exactement l'un de ces liens de ma wishlist :\n" +
            "\n".join(a["url"] for a in articles))}],
        output_format=Selection,
    )
    if r.stop_reason == "refusal" or r.parsed_output is None:
        return None
    return r.parsed_output
