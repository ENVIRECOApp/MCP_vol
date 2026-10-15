"""Serveur MCP de réservation de vols ENVIRECO (Duffel + Airtable)."""

import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from pydantic import BaseModel, EmailStr, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .airtable import (
    CHAMPS_PASSAGER,
    AirtableClient,
    AirtableError,
    echapper_formule,
)
from .config import Config, load_config
from .duffel import DuffelClient, DuffelError, DuffelIncertain
from .formatting import lignes_reservation, resumer_commande, resumer_offre
from .instructions import INSTRUCTIONS

logger = logging.getLogger("serveur_mcp_vol")

CodeIATA = Annotated[
    str, Field(pattern=r"^[A-Za-z]{3}$", description="Code IATA à 3 lettres, ex. CDG")
]
Classe = Literal["economy", "premium_economy", "business", "first"]
TELEPHONE_E164 = r"^\+[1-9]\d{6,14}$"


class PassagerRecherche(BaseModel):
    """Soit {"type": "adult"}, soit {"age": <âge>} pour un mineur."""

    type: Literal["adult"] | None = None
    age: int | None = Field(None, ge=0, le=17)

    @model_validator(mode="after")
    def _un_seul_champ(self) -> "PassagerRecherche":
        if (self.type is None) == (self.age is None):
            raise ValueError('Indiquer soit {"type": "adult"}, soit {"age": <âge>}.')
        return self


class PassagerReservation(BaseModel):
    id: str = Field(description="Identifiant passager Duffel issu de l'offre choisie")
    title: Literal["mr", "mrs", "ms", "miss", "dr"]
    gender: Literal["m", "f"]
    given_name: str = Field(min_length=1)
    family_name: str = Field(min_length=1)
    born_on: date
    email: EmailStr
    phone_number: str = Field(pattern=TELEPHONE_E164)
    infant_passenger_id: str | None = Field(
        None, description="Sur un adulte : id du nourrisson qu'il accompagne"
    )


def _expiree(offre: dict[str, Any]) -> bool:
    expire = offre.get("expires_at")
    if not expire:
        return False
    try:
        limite = datetime.fromisoformat(expire.replace("Z", "+00:00"))
    except ValueError:
        return False
    if limite.tzinfo is None:
        limite = limite.replace(tzinfo=timezone.utc)
    return limite <= datetime.now(timezone.utc)


def create_server(
    duffel: DuffelClient,
    airtable: AirtableClient,
    table_reservations: str,
    table_passagers: str,
    auth: AuthProvider | None = None,
) -> FastMCP:
    mcp = FastMCP(
        name="Réservation de vols ENVIRECO", instructions=INSTRUCTIONS, auth=auth
    )

    # Garde-fous contre une double réservation (retry du client MCP).
    en_cours: set[str] = set()
    incertaines: set[str] = set()
    reservees: dict[str, dict[str, Any]] = {}

    async def _enregistrer(commande: dict[str, Any]) -> dict[str, Any]:
        """Écrit la commande dans reservation_vol sans jamais lever d'exception."""
        try:
            ids = await airtable.creer(table_reservations, lignes_reservation(commande))
        except Exception as e:  # la réservation est payée : ne pas masquer le succès
            logger.exception("Écriture Airtable échouée pour %s", commande.get("id"))
            return {
                "enregistre_dans_airtable": False,
                "erreur_airtable": getattr(e, "message", str(e)),
            }
        return {"enregistre_dans_airtable": True, "lignes_airtable_creees": len(ids)}

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.tool(
        annotations={
            "title": "Rechercher des vols",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def rechercher_vols(
        code_iata_depart: CodeIATA,
        code_iata_arrivee: CodeIATA,
        date_aller: date,
        passagers: Annotated[
            list[PassagerRecherche],
            Field(
                min_length=1,
                max_length=9,
                description='Un élément par passager : {"type": "adult"} (16 ans ou '
                'plus) ou {"age": <âge>} pour un mineur.',
            ),
        ],
        date_retour: Annotated[
            date | None, Field(description="À renseigner pour un aller-retour")
        ] = None,
        classe: Classe = "economy",
        nombre_max_offres: Annotated[int, Field(ge=1, le=30)] = 10,
    ) -> dict[str, Any]:
        """Recherche des offres de vols (aller simple ou aller-retour).

        Renvoie les offres les moins chères, avec pour chacune son offer_id, son
        prix, sa date d'expiration, les identifiants passagers à réutiliser pour
        la réservation, et le détail de chaque trajet. Ne réserve rien.
        """
        depart, arrivee = code_iata_depart.upper(), code_iata_arrivee.upper()
        if date_aller < date.today():
            raise ToolError("La date aller est dans le passé.")
        if date_retour and date_retour < date_aller:
            raise ToolError("La date de retour précède la date aller.")
        slices = [
            {
                "origin": depart,
                "destination": arrivee,
                "departure_date": date_aller.isoformat(),
            }
        ]
        if date_retour:
            slices.append(
                {
                    "origin": arrivee,
                    "destination": depart,
                    "departure_date": date_retour.isoformat(),
                }
            )
        try:
            resultat = await duffel.rechercher_offres(
                slices, [p.model_dump(exclude_none=True) for p in passagers], classe
            )
        except DuffelError as e:
            raise ToolError(f"Recherche Duffel impossible : {e.message}") from e

        offres = sorted(
            resultat.get("offers") or [], key=lambda o: Decimal(o["total_amount"])
        )
        return {
            "nombre_offres_trouvees": len(offres),
            "offres": [resumer_offre(o) for o in offres[:nombre_max_offres]],
        }

    @mcp.tool(
        annotations={
            "title": "Réserver et payer un vol",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def reserver_vol(
        offer_id: Annotated[str, Field(pattern=r"^off_\w+$")],
        passagers: Annotated[list[PassagerReservation], Field(min_length=1)],
        montant_confirme: Annotated[
            str,
            Field(description="Prix total validé par l'utilisateur, ex. \"245.30\""),
        ],
        devise_confirmee: Annotated[
            str, Field(pattern=r"^[A-Za-z]{3}$", description="Devise validée, ex. EUR")
        ],
    ) -> dict[str, Any]:
        """Réserve l'offre et la PAIE immédiatement (paiement réel et irréversible).

        À n'appeler qu'après la validation explicite du montant par l'utilisateur.
        Le serveur refuse si le prix de l'offre ne correspond plus au montant
        confirmé. La réservation est ensuite enregistrée dans la table Airtable
        reservation_vol. Ne jamais relancer cet outil après une erreur.
        """
        if offer_id in reservees:
            return {**reservees[offer_id], "deja_reservee": True}
        if offer_id in incertaines:
            raise ToolError(
                "Une tentative précédente sur cette offre n'a pas reçu de réponse de "
                "Duffel : la réservation a peut-être abouti. Vérifier dans le tableau "
                "de bord Duffel avant toute nouvelle tentative."
            )
        if offer_id in en_cours:
            raise ToolError("Une réservation de cette offre est déjà en cours.")

        try:
            montant = Decimal(montant_confirme.replace(",", ".").strip())
        except InvalidOperation as e:
            raise ToolError("montant_confirme n'est pas un nombre valide.") from e
        devise = devise_confirmee.upper()

        en_cours.add(offer_id)
        try:
            try:
                offre = await duffel.lire_offre(offer_id)
            except DuffelError as e:
                raise ToolError(
                    f"Offre indisponible ({e.message}). Relancer une recherche."
                ) from e
            if _expiree(offre):
                raise ToolError("Cette offre a expiré. Relancer une recherche.")

            prix, devise_offre = offre["total_amount"], offre["total_currency"]
            if Decimal(prix) != montant or devise_offre.upper() != devise:
                raise ToolError(
                    f"Le prix actuel de l'offre est {prix} {devise_offre}, différent "
                    f"du montant confirmé ({montant_confirme} {devise}). Rien n'a été "
                    "réservé : demander une nouvelle confirmation à l'utilisateur."
                )

            attendus = {p["id"] for p in offre.get("passengers") or []}
            fournis = [p.id for p in passagers]
            if set(fournis) != attendus or len(fournis) != len(attendus):
                raise ToolError(
                    "Les identifiants passagers ne correspondent pas à ceux de "
                    f"l'offre. Attendus : {sorted(attendus)}."
                )

            try:
                commande = await duffel.creer_commande(
                    offer_id,
                    [p.model_dump(mode="json", exclude_none=True) for p in passagers],
                    prix,
                    devise_offre,
                )
            except DuffelIncertain as e:
                incertaines.add(offer_id)
                raise ToolError(
                    "Duffel n'a pas répondu à la demande de réservation : son "
                    "résultat est INCONNU (elle a peut-être abouti et été payée). "
                    "Ne pas relancer ; vérifier dans le tableau de bord Duffel."
                ) from e
            except DuffelError as e:
                raise ToolError(
                    f"Réservation refusée par Duffel, rien n'a été payé : {e.message}"
                ) from e

            resultat = {**resumer_commande(commande), **await _enregistrer(commande)}
            reservees[offer_id] = resultat
            return resultat
        finally:
            en_cours.discard(offer_id)

    @mcp.tool(
        annotations={
            "title": "Enregistrer une réservation dans Airtable",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        }
    )
    async def enregistrer_reservation(
        order_id: Annotated[str, Field(pattern=r"^ord_\w+$")],
    ) -> dict[str, Any]:
        """Enregistre dans reservation_vol une réservation Duffel déjà effectuée.

        À utiliser uniquement si reserver_vol a renvoyé enregistre_dans_airtable
        à false. Ne crée aucun doublon et ne déclenche aucun paiement.
        """
        try:
            commande = await duffel.lire_commande(order_id)
        except DuffelError as e:
            raise ToolError(f"Commande introuvable : {e.message}") from e
        reference = commande.get("booking_reference") or ""
        try:
            existantes = await airtable.lister(
                table_reservations,
                formule=f"{{Référence_réservation}}='{echapper_formule(reference)}'",
            )
        except AirtableError as e:
            raise ToolError(e.message) from e
        if existantes:
            return {
                **resumer_commande(commande),
                "enregistre_dans_airtable": True,
                "deja_enregistree": True,
            }
        return {**resumer_commande(commande), **await _enregistrer(commande)}

    @mcp.tool(
        annotations={"title": "Consulter les réservations", "readOnlyHint": True}
    )
    async def consulter_reservations(
        mois: Annotated[
            int, Field(ge=1, le=24, description="Nombre de mois à venir à couvrir")
        ] = 3,
    ) -> dict[str, Any]:
        """Liste les vols déjà réservés (table reservation_vol) sur les mois à venir.

        Les dates sont des heures locales de l'aéroport de départ.
        """
        formule = (
            "AND(IS_AFTER({Date}, DATEADD(NOW(), -1, 'days')), "
            f"IS_BEFORE({{Date}}, DATEADD(NOW(), {mois}, 'months')))"
        )
        try:
            lignes = await airtable.lister(table_reservations, formule=formule, tri="Date")
        except AirtableError as e:
            raise ToolError(e.message) from e
        for ligne in lignes:
            if isinstance(ligne.get("Date"), str):
                ligne["Date"] = re.sub(r"(\.\d+)?Z$", "", ligne["Date"])
        return {"periode_mois": mois, "reservations": lignes}

    @mcp.tool(annotations={"title": "Lister les passagers", "readOnlyHint": True})
    async def lister_passagers(
        prenom: Annotated[
            str | None, Field(description="Filtre optionnel sur le prénom")
        ] = None,
    ) -> dict[str, Any]:
        """Lit les coordonnées des passagers (table Coordonnees_passager_vol)."""
        formule = None
        if prenom and prenom.strip():
            formule = (
                f"FIND(LOWER('{echapper_formule(prenom.strip())}'), LOWER({{Prénom}}))"
            )
        try:
            lignes = await airtable.lister(table_passagers, formule=formule)
        except AirtableError as e:
            raise ToolError(e.message) from e
        return {
            "passagers": [
                {c: ligne.get(c) for c in CHAMPS_PASSAGER} for ligne in lignes
            ]
        }

    @mcp.tool(
        annotations={
            "title": "Ajouter un passager",
            "readOnlyHint": False,
            "destructiveHint": False,
        }
    )
    async def ajouter_passager(
        nom: Annotated[str, Field(min_length=1)],
        prenom: Annotated[str, Field(min_length=1)],
        titre: Literal["mr", "mrs", "ms", "miss"],
        genre: Literal["m", "f"],
        date_naissance: date,
        email: EmailStr,
        telephone: Annotated[
            str,
            Field(pattern=TELEPHONE_E164, description="Format international +336…"),
        ],
    ) -> dict[str, Any]:
        """Ajoute un passager dans la table Airtable Coordonnees_passager_vol.

        Refuse la création si un passager de mêmes nom, prénom et date de
        naissance existe déjà.
        """
        nom, prenom = nom.strip(), prenom.strip()
        if date_naissance >= date.today():
            raise ToolError("La date de naissance doit être dans le passé.")
        formule = (
            f"AND(LOWER({{Nom}})=LOWER('{echapper_formule(nom)}'), "
            f"LOWER({{Prénom}})=LOWER('{echapper_formule(prenom)}'))"
        )
        try:
            homonymes = await airtable.lister(table_passagers, formule=formule)
            if any(
                str(h.get("Date_naissance", ""))[:10] == date_naissance.isoformat()
                for h in homonymes
            ):
                raise ToolError(
                    f"{prenom} {nom} (né(e) le {date_naissance.isoformat()}) existe "
                    "déjà dans la table des passagers."
                )
            champs = {
                "Nom": nom,
                "Prénom": prenom,
                "Titre": titre,
                "Genre": genre,
                "Date_naissance": date_naissance.isoformat(),
                "Email": str(email),
                "Téléphone": telephone,
            }
            # typecast : accepte les valeurs si Titre/Genre sont des listes déroulantes.
            ids = await airtable.creer(table_passagers, [champs], typecast=True)
        except AirtableError as e:
            raise ToolError(e.message) from e
        return {"passager_ajoute": True, "id_airtable": ids[0], **champs}

    return mcp


def build(config: Config) -> FastMCP:
    from .auth import EnvirecoOAuthProvider

    return create_server(
        duffel=DuffelClient(config.duffel_api_key),
        airtable=AirtableClient(config.airtable_token, config.airtable_base_id),
        table_reservations=config.table_reservations,
        table_passagers=config.table_passagers,
        auth=EnvirecoOAuthProvider(config),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    logger.info(
        "Démarrage — Duffel en mode %s", "TEST" if config.duffel_mode_test else "LIVE"
    )
    build(config).run(
        transport="http",
        host="0.0.0.0",
        port=config.port,
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
