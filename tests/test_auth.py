"""Parcours OAuth complet contre l'application HTTP réelle (en mémoire)."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.airtable import AirtableClient
from src.auth import EnvirecoOAuthProvider
from src.config import Config
from src.duffel import DuffelClient
from src.server import create_server

BASE_URL = "https://mcp.exemple.test"
MOT_DE_PASSE = "mot-de-passe-de-test"
REDIRECTION = "https://chatgpt.com/connector_platform_oauth_redirect"
RESSOURCE = f"{BASE_URL}/mcp"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
EN_TETES_MCP = {"Accept": "application/json, text/event-stream"}


def _config(**surcharge) -> Config:
    valeurs = dict(
        duffel_api_key="duffel_test_x",
        airtable_token="pat_x",
        airtable_base_id="appTEST",
        table_reservations="tblRESA",
        table_passagers="Coordonnees_passager_vol",
        mcp_login_password=MOT_DE_PASSE,
        jwt_secret="s" * 40,
        public_base_url=BASE_URL,
        hotes_redirection_autorises=("chatgpt.com", "chat.openai.com"),
        port=8000,
    )
    return Config(**{**valeurs, **surcharge})


def _application(config: Config):
    serveur = create_server(
        duffel=DuffelClient("duffel_test_x"),
        airtable=AirtableClient("pat_x", "appTEST"),
        table_reservations="tblRESA",
        table_passagers="Coordonnees_passager_vol",
        auth=EnvirecoOAuthProvider(config),
    )
    return serveur.http_app(path="/mcp", stateless_http=True)


class Navigateur:
    """Client HTTP branché sur l'application, sans suivi des redirections."""

    def __init__(self, config: Config | None = None):
        self.app = _application(config or _config())

    async def __aenter__(self) -> httpx.AsyncClient:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=BASE_URL
        )
        return self.http

    async def __aexit__(self, *exc) -> None:
        await self.http.aclose()
        await self._lifespan.__aexit__(*exc)


async def _enregistrer(http, redirection=REDIRECTION, methode="none"):
    return await http.post(
        "/register",
        json={
            "client_name": "ChatGPT",
            "redirect_uris": [redirection],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": methode,
        },
    )


async def _demander_autorisation(http, client_id, ressource=RESSOURCE):
    verifieur = secrets.token_urlsafe(48)
    defi = (
        base64.urlsafe_b64encode(hashlib.sha256(verifieur.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECTION,
        "code_challenge": defi,
        "code_challenge_method": "S256",
        "state": "etat-xyz",
    }
    if ressource:
        params["resource"] = ressource
    reponse = await http.get("/authorize", params=params)
    assert reponse.status_code == 302, reponse.text
    page = urlparse(reponse.headers["location"])
    assert page.path == "/login"
    return parse_qs(page.query)["txn"][0], verifieur


async def _obtenir_jetons(http, client, ressource=RESSOURCE):
    txn, verifieur = await _demander_autorisation(http, client["client_id"], ressource)
    reponse = await http.post("/login", data={"txn": txn, "password": MOT_DE_PASSE})
    assert reponse.status_code == 302, reponse.text
    retour = urlparse(reponse.headers["location"])
    assert f"{retour.scheme}://{retour.netloc}{retour.path}" == REDIRECTION
    requete = parse_qs(retour.query)
    assert requete["state"] == ["etat-xyz"]
    donnees = {
        "grant_type": "authorization_code",
        "code": requete["code"][0],
        "redirect_uri": REDIRECTION,
        "client_id": client["client_id"],
        "code_verifier": verifieur,
    }
    if client.get("client_secret"):
        donnees["client_secret"] = client["client_secret"]
    if ressource:
        donnees["resource"] = ressource
    return await http.post("/token", data=donnees), donnees


async def test_decouverte():
    async with Navigateur() as http:
        serveur = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert serveur["issuer"].rstrip("/") == BASE_URL
        assert serveur["registration_endpoint"] == f"{BASE_URL}/register"
        assert "S256" in serveur["code_challenge_methods_supported"]
        assert "refresh_token" in serveur["grant_types_supported"]
        ressource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert ressource["resource"] == RESSOURCE
        assert ressource["scopes_supported"] == ["offline_access"]
        assert (await http.get("/health")).json() == {"status": "ok"}


async def test_mcp_refuse_sans_jeton():
    async with Navigateur() as http:
        reponse = await http.post("/mcp", json=INITIALIZE, headers=EN_TETES_MCP)
        assert reponse.status_code == 401
        assert "resource_metadata" in reponse.headers["www-authenticate"]
        faux = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**EN_TETES_MCP, "Authorization": "Bearer faux"},
        )
        assert faux.status_code == 401


async def test_redirection_inconnue_refusee():
    async with Navigateur() as http:
        reponse = await _enregistrer(http, "https://pirate.exemple/callback")
        assert reponse.status_code == 400
        assert reponse.json()["error"] == "invalid_redirect_uri"
        local = await _enregistrer(http, "http://localhost:6274/oauth/callback")
        assert local.status_code == 400


@pytest.mark.parametrize("methode", ["none", "client_secret_post"])
@pytest.mark.parametrize("ressource", [RESSOURCE, None])
async def test_parcours_complet(methode, ressource):
    async with Navigateur() as http:
        enregistrement = await _enregistrer(http, methode=methode)
        assert enregistrement.status_code == 201, enregistrement.text
        client = enregistrement.json()
        assert bool(client.get("client_secret")) == (methode != "none")

        reponse, donnees = await _obtenir_jetons(http, client, ressource)
        assert reponse.status_code == 200, reponse.text
        jetons = reponse.json()
        assert jetons["token_type"].lower() == "bearer"
        assert jetons["refresh_token"]

        appel = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**EN_TETES_MCP, "Authorization": f"Bearer {jetons['access_token']}"},
        )
        assert appel.status_code == 200, appel.text
        assert "ENVIRECO" in appel.text

        # Un code d'autorisation ne sert qu'une fois.
        rejoue = await http.post("/token", data=donnees)
        assert rejoue.status_code in (400, 401)
        assert "access_token" not in rejoue.text

        # Le jeton de rafraîchissement n'ouvre pas l'accès au serveur MCP.
        detourne = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**EN_TETES_MCP, "Authorization": f"Bearer {jetons['refresh_token']}"},
        )
        assert detourne.status_code == 401

        rafraichi = await http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": jetons["refresh_token"],
                "client_id": client["client_id"],
                **(
                    {"client_secret": client["client_secret"]}
                    if client.get("client_secret")
                    else {}
                ),
            },
        )
        assert rafraichi.status_code == 200, rafraichi.text
        assert rafraichi.json()["access_token"]


async def test_mauvais_mot_de_passe_puis_blocage():
    async with Navigateur() as http:
        client = (await _enregistrer(http)).json()
        txn, _ = await _demander_autorisation(http, client["client_id"])
        page = await http.get("/login", params={"txn": txn})
        assert page.status_code == 200
        assert 'name="password"' in page.text

        for _ in range(5):
            echec = await http.post("/login", data={"txn": txn, "password": "faux"})
            assert echec.status_code == 401
            assert "location" not in echec.headers
        # Après 5 échecs, même le bon mot de passe est refusé pendant 15 minutes.
        bloque = await http.post("/login", data={"txn": txn, "password": MOT_DE_PASSE})
        assert bloque.status_code == 429


async def test_transaction_invalide():
    async with Navigateur() as http:
        assert (await http.get("/login", params={"txn": "n-importe-quoi"})).status_code == 400
        reponse = await http.post(
            "/login", data={"txn": "n-importe-quoi", "password": MOT_DE_PASSE}
        )
        assert reponse.status_code == 400


async def test_mauvais_verifieur_pkce():
    async with Navigateur() as http:
        client = (await _enregistrer(http)).json()
        txn, _ = await _demander_autorisation(http, client["client_id"])
        reponse = await http.post("/login", data={"txn": txn, "password": MOT_DE_PASSE})
        code = parse_qs(urlparse(reponse.headers["location"]).query)["code"][0]
        jetons = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECTION,
                "client_id": client["client_id"],
                "code_verifier": "mauvais-verifieur-" + "x" * 40,
                "resource": RESSOURCE,
            },
        )
        assert jetons.status_code in (400, 401)
        assert "access_token" not in jetons.text


async def test_jetons_survivent_a_un_redemarrage():
    async with Navigateur() as http:
        client = (await _enregistrer(http)).json()
        reponse, _ = await _obtenir_jetons(http, client)
        jetons = reponse.json()

    # Nouvelle instance du serveur : rien n'est conservé en mémoire.
    async with Navigateur() as http:
        appel = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**EN_TETES_MCP, "Authorization": f"Bearer {jetons['access_token']}"},
        )
        assert appel.status_code == 200
        rafraichi = await http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": jetons["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        assert rafraichi.status_code == 200, rafraichi.text

    # Changer le mot de passe invalide tous les jetons émis.
    async with Navigateur(_config(mcp_login_password="un-autre-mot-de-passe")) as http:
        appel = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**EN_TETES_MCP, "Authorization": f"Bearer {jetons['access_token']}"},
        )
        assert appel.status_code == 401
