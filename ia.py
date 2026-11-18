"""
Crush Shopper – l'IA (Claude) pour les suggestions « Pour toi ».

Elle ne s'allume QUE si la clé ANTHROPIC_API_KEY est rangée dans le coffre-fort de GitHub.
Sans clé, le robot utilise son cerveau à règles (suggestions.py) : rien ne casse.

Deux missions :
  1. comprendre mon style et inventer de bonnes recherches pour chaque article ;
  2. choisir, parmi les produits trouvés, ceux qui me ressemblent, avec une phrase perso.
"""
import os
from typing import List

from pydantic import BaseModel

MODELE = "claude-opus-5-5"


def active():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class Recherche(BaseModel):
    pour_url: str          # le lien de l'article de ma wishlist
    requete: str           # ce qu'on tape dans la recherche de Zalando (en français)
    type_produit: str      # mot qui doit apparaître dans le nom (ex. « veste », « rouge à lèvres »)


class Profil(BaseModel):
    style: str             # mon style en une phrase
    recherches: List[Recherche]


class Choix(BaseModel):
    numero: int            # le numéro du produit dans la liste envoyée
    raison: str            # phrase perso courte, en tutoiement


class Selection(BaseModel):
    choix: List[Choix]


def _client():
    import anthropic
    return anthropic.Anthropic()


def profil_et_recherches(articles):
    """articles : liste de dicts {url, nom, option, marque, prix, categorie}."""
    lignes = "\n".join(f"- {a['url']} | {a['nom']} | {a.get('option','')} | {a.get('marque','')} | {a.get('prix','')} | {a.get('categorie','')}"
                       for a in articles)
    r = _client().messages.parse(
        model=MODELE,
        max_tokens=16000,
        output_config={"effort": "low"},
        system=("Tu es la styliste personnelle de l'appli Crush Shopper. Tu analyses une wishlist "
                "et tu prépares des recherches de produits SIMILAIRES chez d'autres marques, comme Google Shopping."),
        messages=[{"role": "user", "content": (
            "Voici ma wishlist (lien | nom | taille ou teinte | marque | prix | catégorie) :\n" + lignes +
            "\n\n1) Décris mon style en une phrase courte, en tutoiement (couleurs, esprit, budget)."
            "\n2) Pour CHAQUE article, propose 2 ou 3 recherches courtes en français (2 à 4 mots) à taper sur Zalando "
            "pour trouver des articles similaires d'AUTRES marques : même type de produit, couleur ou esprit proche. "
            "Donne aussi le mot du type de produit qui doit apparaître dans le nom des résultats (ex. « veste », « rouge à lèvres »).")}],
        output_format=Profil,
    )
    if r.stop_reason == "refusal" or r.parsed_output is None:
        return None
    return r.parsed_output


def choisir(style, candidats, combien=24):
    """candidats : liste de dicts {nom, marque, prix, pour_nom}. Rend {numero: raison}."""
    lignes = "\n".join(f"{i}. {c['nom']} | {c.get('marque','')} | {c.get('prix','')} | proche de : {c['pour_nom']}"
                       for i, c in enumerate(candidats))
    r = _client().messages.parse(
        model=MODELE,
        max_tokens=16000,
        output_config={"effort": "low"},
        system="Tu es la styliste personnelle de l'appli Crush Shopper. Tu parles en tutoiement, avec des phrases courtes et chics.",
        messages=[{"role": "user", "content": (
            f"Mon style : {style}\n\nProduits trouvés (numéro. nom | marque | prix | article de ma wishlist dont il est proche) :\n{lignes}"
            f"\n\nChoisis jusqu'à {combien} produits qui me plairaient vraiment : varie les marques, garde le bon type de produit, "
            "évite les doublons. Pour chacun, écris une raison perso de 15 mots maximum "
            "(ex. « Le même vert olive que ta veste Alo, en version légère et à 69 € »).")}],
        output_format=Selection,
    )
    if r.stop_reason == "refusal" or r.parsed_output is None:
        return None
    return {c.numero: c.raison for c in r.parsed_output.choix}
