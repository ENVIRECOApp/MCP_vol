"""Lecture et validation des variables d'environnement."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

VARIABLES_OBLIGATOIRES = (
    "DUFFEL_API_KEY",
    "AIRTABLE_TOKEN",
    "MCP_LOGIN_PASSWORD",
    "JWT_SECRET",
    "PUBLIC_BASE_URL",
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    duffel_api_key: str
    airtable_token: str
    airtable_base_id: str
    table_reservations: str
    table_passagers: str
    mcp_login_password: str
    jwt_secret: str
    public_base_url: str
    hotes_redirection_autorises: tuple[str, ...]
    port: int

    @property
    def duffel_mode_test(self) -> bool:
        return self.duffel_api_key.startswith("duffel_test_")


def load_config() -> Config:
    load_dotenv()
    manquantes = [v for v in VARIABLES_OBLIGATOIRES if not os.environ.get(v, "").strip()]
    if manquantes:
        raise ConfigError(
            "Variables d'environnement manquantes : " + ", ".join(manquantes)
        )
    if len(os.environ["JWT_SECRET"]) < 32:
        raise ConfigError("JWT_SECRET doit contenir au moins 32 caractères.")
    if len(os.environ["MCP_LOGIN_PASSWORD"]) < 12:
        raise ConfigError("MCP_LOGIN_PASSWORD doit contenir au moins 12 caractères.")

    hotes = os.environ.get("OAUTH_ALLOWED_REDIRECT_HOSTS", "chatgpt.com,chat.openai.com")
    return Config(
        duffel_api_key=os.environ["DUFFEL_API_KEY"].strip(),
        airtable_token=os.environ["AIRTABLE_TOKEN"].strip(),
        airtable_base_id=os.environ.get("AIRTABLE_BASE_ID", "appOaaEm9hZALEfBz").strip(),
        table_reservations=os.environ.get(
            "AIRTABLE_TABLE_RESERVATIONS", "tbltvki9dN6XI1geN"
        ).strip(),
        table_passagers=os.environ.get(
            "AIRTABLE_TABLE_PASSAGERS", "Coordonnees_passager_vol"
        ).strip(),
        mcp_login_password=os.environ["MCP_LOGIN_PASSWORD"],
        jwt_secret=os.environ["JWT_SECRET"],
        public_base_url=os.environ["PUBLIC_BASE_URL"].strip().rstrip("/"),
        hotes_redirection_autorises=tuple(
            h.strip().lower() for h in hotes.split(",") if h.strip()
        ),
        port=int(os.environ.get("PORT", "8000")),
    )
