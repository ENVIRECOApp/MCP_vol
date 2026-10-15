"""Serveur d'autorisation OAuth 2.1 intégré, protégé par un mot de passe unique.

Tout est sans état (jetons signés) : identifiants clients, codes d'autorisation,
jetons d'accès et de rafraîchissement survivent aux redémarrages de Render, qui
n'offre pas de disque persistant. Changer MCP_LOGIN_PASSWORD ou JWT_SECRET
invalide tout ce qui a été émis.
"""

import hashlib
import hmac
import html
import secrets
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

import jwt
from fastmcp.server.auth.auth import (
    AccessToken,
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Config

DUREE_TRANSACTION = 10 * 60
DUREE_CODE = 5 * 60
DUREE_ACCES = 60 * 60
DUREE_RAFRAICHISSEMENT = 90 * 24 * 60 * 60

FENETRE_ECHECS = 15 * 60
MAX_ECHECS_PAR_IP = 5
MAX_ECHECS_GLOBAL = 30

SCOPES = ["offline_access"]
HOTES_LOCAUX = {"localhost", "127.0.0.1"}

PAGE_CONNEXION = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connexion — Réservation de vols ENVIRECO</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f3f4f6;color:#111827;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}}
main{{background:#fff;padding:28px;border-radius:12px;max-width:380px;width:100%;
box-shadow:0 1px 4px rgba(0,0,0,.12)}}
h1{{font-size:1.15rem;margin:0 0 8px}}p{{font-size:.9rem;color:#4b5563;margin:0 0 16px}}
label{{font-size:.85rem;display:block;margin-bottom:6px}}
input{{width:100%;box-sizing:border-box;padding:10px;font-size:1rem;
border:1px solid #9ca3af;border-radius:8px}}
button{{margin-top:16px;width:100%;padding:11px;font-size:1rem;border:0;
border-radius:8px;background:#1d4ed8;color:#fff;cursor:pointer}}
.erreur{{color:#b91c1c;font-size:.9rem;margin:0 0 12px}}
</style></head><body><main>
<h1>Réservation de vols ENVIRECO</h1>
{corps}
</main></body></html>"""

FORMULAIRE = """<p><strong>{client}</strong> ({hote}) demande l'accès au serveur de
réservation. Cet accès permet de réserver et de payer des billets d'avion.</p>
{erreur}
<form method="post" action="/login">
<input type="hidden" name="txn" value="{txn}">
<label for="password">Mot de passe</label>
<input id="password" name="password" type="password" autocomplete="current-password"
required autofocus>
<button type="submit">Autoriser l'accès</button>
</form>"""


def _page(corps: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        PAGE_CONNEXION.format(corps=corps),
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


class LimiteurEchecs:
    """Limite les essais de mot de passe, par adresse IP et globalement."""

    def __init__(self) -> None:
        self._global: deque[float] = deque()
        self._par_ip: dict[str, deque[float]] = {}

    def _purger(self, file: deque[float], maintenant: float) -> None:
        while file and file[0] < maintenant - FENETRE_ECHECS:
            file.popleft()

    def bloque(self, ip: str) -> bool:
        maintenant = time.time()
        self._purger(self._global, maintenant)
        file_ip = self._par_ip.get(ip)
        if file_ip is not None:
            self._purger(file_ip, maintenant)
            if not file_ip:
                del self._par_ip[ip]
        return len(self._global) >= MAX_ECHECS_GLOBAL or (
            file_ip is not None and len(file_ip) >= MAX_ECHECS_PAR_IP
        )

    def enregistrer_echec(self, ip: str) -> None:
        maintenant = time.time()
        self._global.append(maintenant)
        self._par_ip.setdefault(ip, deque()).append(maintenant)


class EnvirecoOAuthProvider(OAuthProvider):
    def __init__(self, config: Config):
        super().__init__(
            base_url=config.public_base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._base = config.public_base_url
        self._mot_de_passe = config.mcp_login_password.encode()
        self._hotes_autorises = set(config.hotes_redirection_autorises)
        self._cle = hashlib.sha256(
            f"{config.jwt_secret}|{config.mcp_login_password}".encode()
        ).digest()
        self._limiteur = LimiteurEchecs()
        # Codes déjà échangés et jetons révoqués (jti -> expiration), en mémoire.
        self._jti_invalides: dict[str, float] = {}

    @property
    def scopes_supported(self) -> list[str]:
        return SCOPES

    # --- Jetons signés -----------------------------------------------------

    def _signer(self, typ: str, contenu: dict[str, Any], duree: int | None) -> str:
        maintenant = int(time.time())
        charge = {**contenu, "typ": typ, "iat": maintenant, "jti": secrets.token_urlsafe(12)}
        if duree is not None:
            charge["exp"] = maintenant + duree
        return jwt.encode(charge, self._cle, algorithm="HS256")

    def _lire(self, jeton: str, typ: str) -> dict[str, Any] | None:
        try:
            charge = jwt.decode(jeton, self._cle, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        if charge.get("typ") != typ or charge.get("jti") in self._jti_invalides:
            return None
        return charge

    def _invalider(self, charge: dict[str, Any]) -> None:
        maintenant = time.time()
        for jti in [j for j, exp in self._jti_invalides.items() if exp < maintenant]:
            del self._jti_invalides[jti]
        self._jti_invalides[charge["jti"]] = charge.get("exp", maintenant + DUREE_RAFRAICHISSEMENT)

    def _secret_client(self, jti: str) -> str:
        return hmac.new(self._cle, f"client:{jti}".encode(), hashlib.sha256).hexdigest()

    # --- Clients (enregistrement dynamique sans état) ------------------------

    def _redirection_autorisee(self, uri: str) -> bool:
        url = urlparse(uri)
        hote = (url.hostname or "").lower()
        if hote not in self._hotes_autorises:
            return False
        return url.scheme == "https" or (url.scheme == "http" and hote in HOTES_LOCAUX)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(self._redirection_autorisee(u) for u in uris):
            raise RegistrationError(
                "invalid_redirect_uri",
                "Seules les adresses de redirection de ChatGPT sont acceptées.",
            )
        # L'identifiant client porte lui-même ses métadonnées, signées.
        client_id = self._signer(
            "client",
            {
                "r": uris,
                "m": client_info.token_endpoint_auth_method,
                "g": client_info.grant_types,
                "s": client_info.scope,
                "n": client_info.client_name,
            },
            duree=None,
        )
        client_info.client_id = client_id
        if client_info.client_secret is not None:
            charge = self._lire(client_id, "client")
            assert charge is not None
            client_info.client_secret = self._secret_client(charge["jti"])

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        charge = self._lire(client_id, "client")
        if charge is None:
            return None
        avec_secret = charge.get("m") != "none"
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=self._secret_client(charge["jti"]) if avec_secret else None,
            client_secret_expires_at=0 if avec_secret else None,
            client_id_issued_at=charge.get("iat"),
            redirect_uris=charge["r"],
            token_endpoint_auth_method=charge.get("m"),
            grant_types=charge.get("g") or ["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=charge.get("s"),
            client_name=charge.get("n"),
        )

    # --- Autorisation : page de mot de passe ---------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = self._signer(
            "txn",
            {
                "cid": client.client_id,
                "ru": str(params.redirect_uri),
                "rex": params.redirect_uri_provided_explicitly,
                "cc": params.code_challenge,
                "st": params.state,
                "sc": params.scopes or [],
                "rs": params.resource,
                "n": client.client_name,
            },
            DUREE_TRANSACTION,
        )
        return f"{self._base}/login?txn={txn}"

    def _formulaire(self, txn: str, charge: dict[str, Any], erreur: str = "", status: int = 200) -> Response:
        return _page(
            FORMULAIRE.format(
                client=html.escape(charge.get("n") or "Une application"),
                hote=html.escape(urlparse(charge["ru"]).hostname or ""),
                erreur=f'<p class="erreur">{html.escape(erreur)}</p>' if erreur else "",
                txn=html.escape(txn, quote=True),
            ),
            status,
        )

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            mot_de_passe = None
        else:
            formulaire = await request.form()
            txn = str(formulaire.get("txn", ""))
            mot_de_passe = str(formulaire.get("password", ""))

        charge = self._lire(txn, "txn")
        if charge is None:
            return _page(
                "<p>Cette demande de connexion a expiré. Relancez la connexion "
                "depuis ChatGPT.</p>",
                400,
            )
        if mot_de_passe is None:
            return self._formulaire(txn, charge)

        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "inconnue"
        )
        if self._limiteur.bloque(ip):
            return self._formulaire(
                txn, charge, "Trop de tentatives. Réessayez dans 15 minutes.", 429
            )
        if not hmac.compare_digest(mot_de_passe.encode(), self._mot_de_passe):
            self._limiteur.enregistrer_echec(ip)
            return self._formulaire(txn, charge, "Mot de passe incorrect.", 401)

        self._invalider(charge)
        code = self._signer(
            "code",
            {k: charge[k] for k in ("cid", "ru", "rex", "cc", "sc", "rs")},
            DUREE_CODE,
        )
        return RedirectResponse(
            construct_redirect_uri(charge["ru"], code=code, state=charge.get("st")),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(Route("/login", endpoint=self._login, methods=["GET", "POST"]))
        return routes

    # --- Codes et jetons -------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        charge = self._lire(authorization_code, "code")
        if charge is None or charge["cid"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=charge["sc"],
            expires_at=charge["exp"],
            client_id=charge["cid"],
            code_challenge=charge["cc"],
            redirect_uri=charge["ru"],
            redirect_uri_provided_explicitly=charge["rex"],
            resource=charge.get("rs"),
            subject="envireco",
        )

    def _emettre(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        contenu = {"cid": client_id, "sc": scopes, "rs": resource, "sub": "envireco"}
        return OAuthToken(
            access_token=self._signer("access", contenu, DUREE_ACCES),
            token_type="Bearer",
            expires_in=DUREE_ACCES,
            refresh_token=self._signer("refresh", contenu, DUREE_RAFRAICHISSEMENT),
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        charge = self._lire(authorization_code.code, "code")
        if charge is None:
            raise TokenError("invalid_grant", "Code d'autorisation invalide ou déjà utilisé.")
        self._invalider(charge)
        return self._emettre(
            authorization_code.client_id,
            authorization_code.scopes,
            authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        charge = self._lire(refresh_token, "refresh")
        if charge is None or charge["cid"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=charge["cid"],
            scopes=charge["sc"],
            expires_at=charge["exp"],
            resource=charge.get("rs"),
            subject=charge.get("sub"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(refresh_token.scopes):
            raise TokenError("invalid_scope", "Scopes demandés non autorisés.")
        # L'ancien jeton de rafraîchissement reste valable jusqu'à son expiration :
        # sans état persistant, le révoquer ici ferait échouer un nouvel essai du
        # client après une réponse perdue.
        return self._emettre(
            refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        charge = self._lire(token, "access")
        if charge is None:
            return None
        return AccessToken(
            token=token,
            client_id=charge["cid"],
            scopes=charge["sc"],
            expires_at=charge["exp"],
            resource=charge.get("rs"),
            subject=charge.get("sub"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for typ in ("access", "refresh"):
            charge = self._lire(token.token, typ)
            if charge is not None:
                self._invalider(charge)
