import copy
from datetime import date, datetime, timedelta, timezone

import pytest
import respx
from fastmcp import Client

from src.airtable import AirtableClient
from src.duffel import DuffelClient
from src.server import create_server

BASE = "appTEST"
TABLE_RESERVATIONS = "tblRESA"
TABLE_PASSAGERS = "Coordonnees_passager_vol"
URL_RESERVATIONS = f"https://api.airtable.com/v0/{BASE}/{TABLE_RESERVATIONS}"
URL_PASSAGERS = f"https://api.airtable.com/v0/{BASE}/{TABLE_PASSAGERS}"
DUFFEL = "https://api.duffel.com"

DEMAIN = date.today() + timedelta(days=30)


def _segment(origine, destination, depart, arrivee, compagnie="Duffel Airways"):
    return {
        "origin": {"iata_code": origine, "city_name": f"Ville {origine}"},
        "destination": {"iata_code": destination, "city_name": f"Ville {destination}"},
        "departing_at": depart,
        "arriving_at": arrivee,
        "marketing_carrier": {"name": compagnie, "iata_code": "ZZ"},
        "operating_carrier": {"name": compagnie, "iata_code": "ZZ"},
        "marketing_carrier_flight_number": "123",
        "passengers": [
            {
                "baggages": [
                    {"type": "carry_on", "quantity": 1},
                    {"type": "checked", "quantity": 1},
                ]
            }
        ],
    }


def _slice(origine, destination, segments):
    return {
        "origin": {"iata_code": origine, "city_name": f"Ville {origine}"},
        "destination": {"iata_code": destination, "city_name": f"Ville {destination}"},
        "duration": "PT5H",
        "fare_brand_name": "Basic",
        "segments": segments,
    }


OFFRE = {
    "id": "off_123",
    "total_amount": "245.30",
    "total_currency": "EUR",
    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    "payment_requirements": {"requires_instant_payment": False},
    "passengers": [{"id": "pas_1", "type": "adult"}, {"id": "pas_2", "age": 14}],
    "slices": [
        _slice(
            "CDG",
            "JFK",
            [
                _segment("CDG", "LHR", "2026-12-01T06:05:00", "2026-12-01T06:30:00"),
                _segment(
                    "LHR", "JFK", "2026-12-01T08:00:00", "2026-12-01T11:00:00", "Autre Air"
                ),
            ],
        ),
        _slice(
            "JFK",
            "CDG",
            [_segment("JFK", "CDG", "2026-12-10T18:00:00", "2026-12-11T07:00:00")],
        ),
    ],
}

COMMANDE = {
    "id": "ord_123",
    "booking_reference": "ABC123",
    "total_amount": "245.30",
    "total_currency": "EUR",
    "payment_status": {"awaiting_payment": False},
    "documents": [{"unique_identifier": "TKT1"}, {"unique_identifier": "TKT2"}],
    "passengers": [
        {"given_name": "Jean", "family_name": "Dupont"},
        {"given_name": "Léa", "family_name": "Dupont"},
    ],
    "slices": OFFRE["slices"],
}

PASSAGERS = [
    {
        "id": "pas_1",
        "title": "mr",
        "gender": "m",
        "given_name": "Jean",
        "family_name": "Dupont",
        "born_on": "1985-03-12",
        "email": "jean.dupont@exemple.com",
        "phone_number": "+33612345678",
    },
    {
        "id": "pas_2",
        "title": "miss",
        "gender": "f",
        "given_name": "Léa",
        "family_name": "Dupont",
        "born_on": "2012-06-01",
        "email": "jean.dupont@exemple.com",
        "phone_number": "+33612345678",
    },
]


@pytest.fixture
def offre():
    return copy.deepcopy(OFFRE)


@pytest.fixture
def commande():
    return copy.deepcopy(COMMANDE)


@pytest.fixture
def passagers():
    return copy.deepcopy(PASSAGERS)


@pytest.fixture
def http():
    with respx.mock(assert_all_called=False) as routeur:
        yield routeur


@pytest.fixture
async def client(http):
    serveur = create_server(
        duffel=DuffelClient("duffel_test_x"),
        airtable=AirtableClient("pat_x", BASE),
        table_reservations=TABLE_RESERVATIONS,
        table_passagers=TABLE_PASSAGERS,
    )
    async with Client(serveur) as c:
        yield c
