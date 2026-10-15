"""Client HTTP de l'API Duffel (recherche d'offres et commandes)."""

from typing import Any

import httpx

DUFFEL_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"
# Délai laissé aux compagnies pour répondre à une recherche (ms).
SUPPLIER_TIMEOUT_MS = 20000


class DuffelError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class DuffelIncertain(DuffelError):
    """La requête est partie mais aucune réponse n'est revenue : résultat inconnu."""


def _message_erreur(reponse: httpx.Response) -> tuple[str, str | None]:
    try:
        erreurs = reponse.json().get("errors") or []
    except ValueError:
        erreurs = []
    if not erreurs:
        return f"Erreur Duffel HTTP {reponse.status_code}", None
    textes = [
        " : ".join(t for t in (e.get("title"), e.get("message")) if t) for e in erreurs
    ]
    return " | ".join(textes), erreurs[0].get("code")


class DuffelClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Duffel-Version": DUFFEL_VERSION,
            "Accept": "application/json",
        }

    async def _requete(self, methode: str, chemin: str, **kwargs: Any) -> dict[str, Any]:
        try:
            reponse = await self._http.request(
                methode, f"{DUFFEL_URL}{chemin}", headers=self._headers, **kwargs
            )
        except httpx.ConnectError as e:
            raise DuffelError(f"Impossible de joindre Duffel : {e}") from e
        except httpx.HTTPError as e:
            raise DuffelIncertain(
                f"Aucune réponse de Duffel ({type(e).__name__})."
            ) from e
        if reponse.status_code >= 400:
            message, code = _message_erreur(reponse)
            raise DuffelError(message, status=reponse.status_code, code=code)
        return reponse.json()["data"]

    async def rechercher_offres(
        self,
        slices: list[dict[str, str]],
        passagers: list[dict[str, Any]],
        classe: str,
    ) -> dict[str, Any]:
        return await self._requete(
            "POST",
            "/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": SUPPLIER_TIMEOUT_MS},
            json={
                "data": {
                    "slices": slices,
                    "passengers": passagers,
                    "cabin_class": classe,
                }
            },
        )

    async def lire_offre(self, offer_id: str) -> dict[str, Any]:
        return await self._requete("GET", f"/air/offers/{offer_id}")

    async def creer_commande(
        self,
        offer_id: str,
        passagers: list[dict[str, Any]],
        montant: str,
        devise: str,
    ) -> dict[str, Any]:
        return await self._requete(
            "POST",
            "/air/orders",
            json={
                "data": {
                    "type": "instant",
                    "selected_offers": [offer_id],
                    "passengers": passagers,
                    "payments": [
                        {"type": "balance", "currency": devise, "amount": montant}
                    ],
                }
            },
        )

    async def lire_commande(self, order_id: str) -> dict[str, Any]:
        return await self._requete("GET", f"/air/orders/{order_id}")
