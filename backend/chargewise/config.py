"""Application configuration.

Values come from environment variables (loaded from a local .env in development).
In Azure, secrets live in **Azure Key Vault** and are surfaced to the container as
environment variables via Key Vault references / Container Apps secrets — so this
module never needs to talk to Key Vault directly, and no secret is ever committed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./data/chargewise.sqlite"

    # Auth (internet-facing dashboard uses OAuth via Microsoft/Google)
    auth_disabled: bool = True          # True for local dev/tests; False in production
    oauth_provider: str = "microsoft"   # microsoft | google
    oauth_client_id: str = ""
    session_secret: str = "change-me"

    # Octopus
    octopus_api_key: str = ""           # secret -> Key Vault
    octopus_account_number: str = ""

    # TeslaFi
    teslafi_token: str = ""             # secret -> Key Vault

    # Cost settings
    away_rate_gbp_per_kwh: float = 0.50
    petrol_mpg: float = 30.0
    fuel_type: str = "petrol"
    exclude_standing_charge: bool = True

    # Azure
    key_vault_name: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
