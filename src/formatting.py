"""Mise en forme compacte des offres et commandes Duffel."""

from typing import Any

from .airtable import CHAMPS_RESERVATION


def _ville(lieu: dict[str, Any] | None) -> str | None:
    lieu = lieu or {}
    return lieu.get("city_name") or lieu.get("name") or lieu.get("iata_code")


def _distincts(valeurs: list[str | None]) -> list[str]:
    vus: list[str] = []
    for v in valeurs:
        if v and v not in vus:
            vus.append(v)
    return vus


def _bagages(segments: list[dict[str, Any]]) -> list[str]:
    """Bagages inclus pour un passager sur le premier segment du trajet."""
    if not segments:
        return []
    passagers = segments[0].get("passengers") or []
    if not passagers:
        return []
    return [
        f"{b.get('quantity', 1)} x {b.get('type')}"
        for b in passagers[0].get("baggages") or []
        if b.get("quantity", 1)
    ]


def resumer_trajet(slice_: dict[str, Any]) -> dict[str, Any]:
    segments = slice_.get("segments") or []
    premier = segments[0] if segments else {}
    dernier = segments[-1] if segments else {}
    return {
        "depart": _ville(slice_.get("origin")),
        "aeroport_depart": (premier.get("origin") or {}).get("iata_code"),
        "arrivee": _ville(slice_.get("destination")),
        "aeroport_arrivee": (dernier.get("destination") or {}).get("iata_code"),
        "heure_depart": premier.get("departing_at"),
        "heure_arrivee": dernier.get("arriving_at"),
        "duree": slice_.get("duration"),
        "nombre_escales": max(len(segments) - 1, 0),
        "escales": [
            (s.get("destination") or {}).get("iata_code") for s in segments[:-1]
        ],
        "compagnies": _distincts(
            [(s.get("marketing_carrier") or {}).get("name") for s in segments]
        ),
        "vols": [
            f"{(s.get('marketing_carrier') or {}).get('iata_code') or ''}"
            f"{s.get('marketing_carrier_flight_number') or ''}"
            for s in segments
        ],
        "tarif": slice_.get("fare_brand_name"),
        "bagages_inclus": _bagages(segments),
    }


def resumer_offre(offre: dict[str, Any]) -> dict[str, Any]:
    return {
        "offer_id": offre.get("id"),
        "prix_total": offre.get("total_amount"),
        "devise": offre.get("total_currency"),
        "expire_le": offre.get("expires_at"),
        "paiement_instantane_requis": (offre.get("payment_requirements") or {}).get(
            "requires_instant_payment"
        ),
        "passagers": [
            {k: p.get(k) for k in ("id", "type", "age") if p.get(k) is not None}
            for p in offre.get("passengers") or []
        ],
        "trajets": [resumer_trajet(s) for s in offre.get("slices") or []],
    }


def references_tickets(commande: dict[str, Any]) -> list[str]:
    return _distincts(
        [d.get("unique_identifier") for d in commande.get("documents") or []]
    )


def lignes_reservation(commande: dict[str, Any]) -> list[dict[str, Any]]:
    """Enregistrements reservation_vol : un par trajet de la commande."""
    prenoms = ", ".join(
        _distincts([p.get("given_name") for p in commande.get("passengers") or []])
    )
    tickets = ", ".join(references_tickets(commande))
    lignes = []
    for slice_ in commande.get("slices") or []:
        segments = slice_.get("segments") or []
        depart = segments[0].get("departing_at") if segments else None
        compagnies = _distincts(
            [
                (s.get("operating_carrier") or s.get("marketing_carrier") or {}).get(
                    "name"
                )
                for s in segments
            ]
        )
        ligne = {
            CHAMPS_RESERVATION["Départ"]: _ville(slice_.get("origin")),
            CHAMPS_RESERVATION["Arrivée"]: _ville(slice_.get("destination")),
            CHAMPS_RESERVATION["Prénom"]: prenoms,
            CHAMPS_RESERVATION["Compagnie"]: ", ".join(compagnies),
            CHAMPS_RESERVATION["Référence_réservation"]: commande.get(
                "booking_reference"
            ),
            CHAMPS_RESERVATION["Référence_ticket"]: tickets,
        }
        if depart:
            # Heure locale de l'aéroport de départ, stockée telle quelle (sans
            # conversion de fuseau), comme le faisait le scénario Make.
            ligne[CHAMPS_RESERVATION["Date"]] = (
                depart if depart.endswith("Z") else f"{depart}.000Z"
            )
        lignes.append(ligne)
    return lignes


def resumer_commande(commande: dict[str, Any]) -> dict[str, Any]:
    en_attente = (commande.get("payment_status") or {}).get("awaiting_payment")
    return {
        "order_id": commande.get("id"),
        "reference_reservation": commande.get("booking_reference"),
        "references_tickets": references_tickets(commande),
        "montant_total": commande.get("total_amount"),
        "devise": commande.get("total_currency"),
        "paiement_effectue": en_attente is False,
        "passagers": [
            f"{p.get('given_name')} {p.get('family_name')}"
            for p in commande.get("passengers") or []
        ],
        "trajets": [resumer_trajet(s) for s in commande.get("slices") or []],
    }
