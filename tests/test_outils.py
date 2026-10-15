import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from src.airtable import CHAMPS_RESERVATION

from .conftest import DEMAIN, DUFFEL, URL_PASSAGERS, URL_RESERVATIONS


def _corps(appel):
    return json.loads(appel.request.content)


def _reservation(passagers, **surcharge):
    return {
        "offer_id": "off_123",
        "passagers": passagers,
        "montant_confirme": "245.30",
        "devise_confirmee": "EUR",
        **surcharge,
    }


async def test_recherche_aller_simple(client, http, offre):
    chere = {**offre, "id": "off_chere", "total_amount": "900.00"}
    route = http.post(f"{DUFFEL}/air/offer_requests").respond(
        json={"data": {"offers": [chere, offre]}}
    )
    resultat = await client.call_tool(
        "rechercher_vols",
        {
            "code_iata_depart": "cdg",
            "code_iata_arrivee": "JFK",
            "date_aller": DEMAIN.isoformat(),
            "passagers": [{"type": "adult"}, {"age": 14}],
        },
    )
    envoye = _corps(route.calls.last)["data"]
    assert envoye == {
        "slices": [
            {"origin": "CDG", "destination": "JFK", "departure_date": DEMAIN.isoformat()}
        ],
        "passengers": [{"type": "adult"}, {"age": 14}],
        "cabin_class": "economy",
    }
    assert route.calls.last.request.headers["Duffel-Version"] == "v2"
    assert route.calls.last.request.url.params["return_offers"] == "true"

    offres = resultat.data["offres"]
    assert [o["offer_id"] for o in offres] == ["off_123", "off_chere"]
    aller = offres[0]["trajets"][0]
    # Heure d'arrivée = celle du dernier segment (le blueprint Make se trompait).
    assert aller["heure_depart"] == "2026-12-01T06:05:00"
    assert aller["heure_arrivee"] == "2026-12-01T11:00:00"
    assert aller["nombre_escales"] == 1
    assert aller["escales"] == ["LHR"]
    assert aller["compagnies"] == ["Duffel Airways", "Autre Air"]
    assert aller["bagages_inclus"] == ["1 x carry_on", "1 x checked"]
    assert offres[0]["passagers"] == [
        {"id": "pas_1", "type": "adult"},
        {"id": "pas_2", "age": 14},
    ]


async def test_recherche_aller_retour_et_classe(client, http, offre):
    route = http.post(f"{DUFFEL}/air/offer_requests").respond(
        json={"data": {"offers": [offre]}}
    )
    retour = DEMAIN.replace(day=min(DEMAIN.day, 28))
    await client.call_tool(
        "rechercher_vols",
        {
            "code_iata_depart": "CDG",
            "code_iata_arrivee": "JFK",
            "date_aller": retour.isoformat(),
            "date_retour": retour.isoformat(),
            "passagers": [{"type": "adult"}],
            "classe": "business",
        },
    )
    envoye = _corps(route.calls.last)["data"]
    assert envoye["cabin_class"] == "business"
    assert envoye["slices"][1] == {
        "origin": "JFK",
        "destination": "CDG",
        "departure_date": retour.isoformat(),
    }


async def test_recherche_passager_invalide(client, http):
    with pytest.raises(ToolError):
        await client.call_tool(
            "rechercher_vols",
            {
                "code_iata_depart": "CDG",
                "code_iata_arrivee": "JFK",
                "date_aller": DEMAIN.isoformat(),
                "passagers": [{"type": "adult", "age": 14}],
            },
        )


async def test_reservation_reussie(client, http, offre, commande, passagers):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders").respond(json={"data": commande})
    airtable = http.post(URL_RESERVATIONS).respond(
        json={"records": [{"id": "rec1"}, {"id": "rec2"}]}
    )
    resultat = (await client.call_tool("reserver_vol", _reservation(passagers))).data

    envoye = _corps(ordre.calls.last)["data"]
    assert envoye["type"] == "instant"
    assert envoye["selected_offers"] == ["off_123"]
    assert envoye["payments"] == [
        {"type": "balance", "currency": "EUR", "amount": "245.30"}
    ]
    assert envoye["passengers"][0] == passagers[0]

    assert resultat["reference_reservation"] == "ABC123"
    assert resultat["references_tickets"] == ["TKT1", "TKT2"]
    assert resultat["paiement_effectue"] is True
    assert resultat["enregistre_dans_airtable"] is True

    lignes = [r["fields"] for r in _corps(airtable.calls.last)["records"]]
    assert len(lignes) == 2  # un enregistrement par trajet
    aller, retour = lignes
    assert aller[CHAMPS_RESERVATION["Date"]] == "2026-12-01T06:05:00.000Z"
    assert aller[CHAMPS_RESERVATION["Départ"]] == "Ville CDG"
    assert aller[CHAMPS_RESERVATION["Arrivée"]] == "Ville JFK"
    assert aller[CHAMPS_RESERVATION["Prénom"]] == "Jean, Léa"
    assert aller[CHAMPS_RESERVATION["Compagnie"]] == "Duffel Airways, Autre Air"
    assert aller[CHAMPS_RESERVATION["Référence_réservation"]] == "ABC123"
    assert aller[CHAMPS_RESERVATION["Référence_ticket"]] == "TKT1, TKT2"
    assert retour[CHAMPS_RESERVATION["Départ"]] == "Ville JFK"
    assert retour[CHAMPS_RESERVATION["Date"]] == "2026-12-10T18:00:00.000Z"


async def test_nouvel_appel_ne_reserve_pas_deux_fois(
    client, http, offre, commande, passagers
):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders").respond(json={"data": commande})
    http.post(URL_RESERVATIONS).respond(json={"records": [{"id": "rec1"}]})
    await client.call_tool("reserver_vol", _reservation(passagers))
    second = (await client.call_tool("reserver_vol", _reservation(passagers))).data
    assert ordre.call_count == 1
    assert second["deja_reservee"] is True
    assert second["reference_reservation"] == "ABC123"


async def test_prix_modifie_bloque_la_reservation(client, http, offre, passagers):
    offre["total_amount"] = "260.00"
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders")
    with pytest.raises(ToolError, match="260.00 EUR"):
        await client.call_tool("reserver_vol", _reservation(passagers))
    assert not ordre.called


async def test_devise_differente_bloque_la_reservation(client, http, offre, passagers):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders")
    with pytest.raises(ToolError):
        await client.call_tool(
            "reserver_vol", _reservation(passagers, devise_confirmee="USD")
        )
    assert not ordre.called


async def test_offre_expiree(client, http, offre, passagers):
    offre["expires_at"] = "2020-01-01T00:00:00Z"
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders")
    with pytest.raises(ToolError, match="expiré"):
        await client.call_tool("reserver_vol", _reservation(passagers))
    assert not ordre.called


async def test_identifiants_passagers_incoherents(client, http, offre, passagers):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders")
    passagers[1]["id"] = "pas_inconnu"
    with pytest.raises(ToolError, match="identifiants passagers"):
        await client.call_tool("reserver_vol", _reservation(passagers))
    assert not ordre.called


async def test_refus_duffel(client, http, offre, passagers):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    http.post(f"{DUFFEL}/air/orders").respond(
        422,
        json={
            "errors": [
                {
                    "title": "Insufficient balance",
                    "message": "Not enough funds",
                    "code": "insufficient_balance",
                }
            ]
        },
    )
    with pytest.raises(ToolError, match="Insufficient balance : Not enough funds"):
        await client.call_tool("reserver_vol", _reservation(passagers))


async def test_reponse_perdue_bloque_les_relances(client, http, offre, passagers):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with pytest.raises(ToolError, match="INCONNU"):
        await client.call_tool("reserver_vol", _reservation(passagers))
    with pytest.raises(ToolError, match="tableau de bord Duffel"):
        await client.call_tool("reserver_vol", _reservation(passagers))
    assert ordre.call_count == 1


async def test_airtable_en_panne_ne_masque_pas_la_reservation(
    client, http, offre, commande, passagers
):
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    http.post(f"{DUFFEL}/air/orders").respond(json={"data": commande})
    http.post(URL_RESERVATIONS).respond(
        403, json={"error": {"type": "FORBIDDEN", "message": "Accès refusé"}}
    )
    resultat = (await client.call_tool("reserver_vol", _reservation(passagers))).data
    assert resultat["reference_reservation"] == "ABC123"
    assert resultat["paiement_effectue"] is True
    assert resultat["enregistre_dans_airtable"] is False
    assert "Accès refusé" in resultat["erreur_airtable"]


async def test_enregistrer_reservation_rattrapage(client, http, commande):
    http.get(f"{DUFFEL}/air/orders/ord_123").respond(json={"data": commande})
    lecture = http.get(URL_RESERVATIONS).respond(json={"records": []})
    ecriture = http.post(URL_RESERVATIONS).respond(
        json={"records": [{"id": "rec1"}, {"id": "rec2"}]}
    )
    resultat = (
        await client.call_tool("enregistrer_reservation", {"order_id": "ord_123"})
    ).data
    assert (
        lecture.calls.last.request.url.params["filterByFormula"]
        == "{Référence_réservation}='ABC123'"
    )
    assert ecriture.call_count == 1
    assert resultat["enregistre_dans_airtable"] is True
    assert resultat["lignes_airtable_creees"] == 2


async def test_enregistrer_reservation_sans_doublon(client, http, commande):
    http.get(f"{DUFFEL}/air/orders/ord_123").respond(json={"data": commande})
    http.get(URL_RESERVATIONS).respond(
        json={"records": [{"id": "rec1", "fields": {"Départ": "Paris"}}]}
    )
    ecriture = http.post(URL_RESERVATIONS)
    resultat = (
        await client.call_tool("enregistrer_reservation", {"order_id": "ord_123"})
    ).data
    assert not ecriture.called
    assert resultat["deja_enregistree"] is True


async def test_consulter_reservations(client, http):
    lecture = http.get(URL_RESERVATIONS).respond(
        json={
            "records": [
                {
                    "id": "rec1",
                    "fields": {"Date": "2026-12-01T06:05:00.000Z", "Départ": "Paris"},
                }
            ]
        }
    )
    resultat = (await client.call_tool("consulter_reservations", {"mois": 6})).data
    assert "DATEADD(NOW(), 6, 'months')" in (
        lecture.calls.last.request.url.params["filterByFormula"]
    )
    assert resultat["reservations"] == [
        {"Date": "2026-12-01T06:05:00", "Départ": "Paris"}
    ]


async def test_lister_passagers_pagine(client, http):
    http.get(URL_PASSAGERS).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": [{"fields": {"Nom": "Dupont", "Prénom": "Jean"}}],
                    "offset": "suite",
                },
            ),
            httpx.Response(
                200, json={"records": [{"fields": {"Nom": "Martin", "Prénom": "Léa"}}]}
            ),
        ]
    )
    resultat = (await client.call_tool("lister_passagers", {})).data
    assert [p["Nom"] for p in resultat["passagers"]] == ["Dupont", "Martin"]
    assert resultat["passagers"][0]["Email"] is None


NOUVEAU = {
    "nom": "O'Brien",
    "prenom": "Mathis",
    "titre": "mr",
    "genre": "m",
    "date_naissance": "1990-05-20",
    "email": "mathis@exemple.com",
    "telephone": "+33611223344",
}


async def test_ajouter_passager(client, http):
    lecture = http.get(URL_PASSAGERS).respond(json={"records": []})
    ecriture = http.post(URL_PASSAGERS).respond(json={"records": [{"id": "recNEW"}]})
    resultat = (await client.call_tool("ajouter_passager", NOUVEAU)).data
    # L'apostrophe du nom est échappée dans la formule Airtable.
    assert "O\\'Brien" in lecture.calls.last.request.url.params["filterByFormula"]
    envoye = _corps(ecriture.calls.last)
    assert envoye["typecast"] is True
    assert envoye["records"][0]["fields"] == {
        "Nom": "O'Brien",
        "Prénom": "Mathis",
        "Titre": "mr",
        "Genre": "m",
        "Date_naissance": "1990-05-20",
        "Email": "mathis@exemple.com",
        "Téléphone": "+33611223344",
    }
    assert resultat["id_airtable"] == "recNEW"


async def test_ajouter_passager_doublon(client, http):
    http.get(URL_PASSAGERS).respond(
        json={"records": [{"fields": {"Date_naissance": "1990-05-20"}}]}
    )
    ecriture = http.post(URL_PASSAGERS)
    with pytest.raises(ToolError, match="existe"):
        await client.call_tool("ajouter_passager", NOUVEAU)
    assert not ecriture.called


async def test_ajouter_passager_telephone_invalide(client, http):
    with pytest.raises(ToolError):
        await client.call_tool("ajouter_passager", {**NOUVEAU, "telephone": "0611223344"})
