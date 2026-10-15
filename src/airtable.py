"""Client HTTP de l'API Airtable et schéma des deux tables utilisées."""

from typing import Any

import httpx

AIRTABLE_URL = "https://api.airtable.com/v0"

# Table reservation_vol : identifiants de champs repris du scénario Make.
CHAMPS_RESERVATION = {
    "Date": "fldcZGbHJSpxiJlgP",
    "Départ": "fldNgHLTrv32lLvpJ",
    "Arrivée": "fld5Z8DjIZeUFTbcd",
    "Prénom": "fldT4i5Jfhcq3QWan",
    "Compagnie": "fldQ0N7OhzPjf2bo1",
    "Référence_réservation": "fldFDRLfZJSTXFcDY",
    "Référence_ticket": "fldKMMVdnBQTKJWqF",
}

# Table Coordonnees_passager_vol : noms de champs repris de l'instruction de
# l'agent Make (à confirmer sur la base réelle, voir README).
CHAMPS_PASSAGER = (
    "Nom",
    "Prénom",
    "Titre",
    "Genre",
    "Date_naissance",
    "Email",
    "Téléphone",
)


class AirtableError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def echapper_formule(valeur: str) -> str:
    """Échappe une valeur insérée entre apostrophes dans une formule Airtable."""
    return valeur.replace("\\", "\\\\").replace("'", "\\'")


class AirtableClient:
    def __init__(self, token: str, base_id: str, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._base = f"{AIRTABLE_URL}/{base_id}"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _requete(self, methode: str, table: str, **kwargs: Any) -> dict[str, Any]:
        try:
            reponse = await self._http.request(
                methode, f"{self._base}/{table}", headers=self._headers, **kwargs
            )
        except httpx.HTTPError as e:
            raise AirtableError(f"Airtable injoignable ({type(e).__name__}).") from e
        if reponse.status_code >= 400:
            try:
                erreur = reponse.json().get("error")
            except ValueError:
                erreur = None
            if isinstance(erreur, dict):
                detail = erreur.get("message") or erreur.get("type")
            else:
                detail = erreur
            raise AirtableError(
                f"Erreur Airtable HTTP {reponse.status_code}"
                + (f" : {detail}" if detail else ""),
                status=reponse.status_code,
            )
        return reponse.json()

    async def lister(
        self,
        table: str,
        formule: str | None = None,
        tri: str | None = None,
    ) -> list[dict[str, Any]]:
        """Renvoie les champs (indexés par nom) de tous les enregistrements."""
        params: dict[str, Any] = {"pageSize": 100}
        if formule:
            params["filterByFormula"] = formule
        if tri:
            params["sort[0][field]"] = tri
            params["sort[0][direction]"] = "asc"
        lignes: list[dict[str, Any]] = []
        while True:
            page = await self._requete("GET", table, params=params)
            lignes.extend(r.get("fields", {}) for r in page.get("records", []))
            if not page.get("offset"):
                return lignes
            params["offset"] = page["offset"]

    async def creer(
        self, table: str, enregistrements: list[dict[str, Any]], typecast: bool = False
    ) -> list[str]:
        """Crée les enregistrements et renvoie leurs identifiants."""
        ids: list[str] = []
        # L'API Airtable accepte au plus 10 enregistrements par appel.
        for i in range(0, len(enregistrements), 10):
            page = await self._requete(
                "POST",
                table,
                json={
                    "records": [{"fields": f} for f in enregistrements[i : i + 10]],
                    "typecast": typecast,
                },
            )
            ids.extend(r["id"] for r in page.get("records", []))
        return ids
