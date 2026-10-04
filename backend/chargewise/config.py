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
    # utf-8-sig: a .env saved with a byte-order mark still yields its first key.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8-sig", extra="ignore"
    )

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

    # Vehicle display names by VIN: "VIN=Name;VIN=Name" (see parse_vehicle_map).
    # Set it in .env.
    vehicle_map: str = ""

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


_QUOTES = "\"'"
_VEHICLE_MAP_FORM = 'Expected VEHICLE_MAP="VIN1=Name 1;VIN2=Name 2".'


def _strip(text: str) -> str:
    """Strip whitespace and a leading byte-order mark."""
    return text.strip().removeprefix("\ufeff").strip()


def _has_edge_quote(text: str) -> bool:
    """True if ``text`` starts or ends with a quote character."""
    return bool(text) and (text[0] in _QUOTES or text[-1] in _QUOTES)


def parse_vehicle_map(value: str) -> dict[str, str]:
    """Parse the ``VEHICLE_MAP`` setting into ``{VIN: display name}``.

    Format: ``VIN=Name`` entries separated by semicolons, for example
    ``VEHICLE_MAP="VIN1=Name 1;VIN2=Name 2"``. VINs are upper-cased, so the map
    matches however the VIN was typed. Whitespace around entries, VINs and
    names is ignored, as are a leading byte-order mark, empty entries (a
    trailing ``;``) and one pair of quotes around the whole value (some
    env-file readers pass them through).

    A name may contain spaces, commas and apostrophes. It cannot contain
    ``;``, ``=`` or a double quote, and cannot start or end with an apostrophe
    or quote mark (that position is where quoting mistakes show up).

    An empty value gives an empty map. Anything else that is not a list of
    ``VIN=Name`` pairs raises ``ValueError``: an entry with no ``=`` or more
    than one (which is also what a wrong separator such as a comma produces),
    an empty VIN or name, a repeated VIN, a quote mark at one end of the value
    only, or a quote mark where a name or VIN cannot have one. The message
    gives the entry's number and the reason and leaves out its text, because
    it is written to the container log and to ``/api/status``.
    """
    text = _strip(value)
    if _has_edge_quote(text):
        if len(text) < 2 or text[0] != text[-1]:
            raise ValueError(
                "VEHICLE_MAP has a quote mark at one end only. Put one pair of quotes "
                "round the whole value, or none; a name cannot start or end with an "
                f"apostrophe. {_VEHICLE_MAP_FORM}"
            )
        text = _strip(text[1:-1])

    mapping: dict[str, str] = {}
    first_seen: dict[str, int] = {}
    position = 0
    for raw in text.split(";"):
        entry = _strip(raw)
        if not entry:
            continue
        position += 1
        vin, sep, name = entry.partition("=")
        vin, name = _strip(vin).upper(), name.strip()
        if '"' in entry or _has_edge_quote(vin) or _has_edge_quote(name):
            reason = (
                "has a quote mark where none is allowed (no double quote in a VIN or "
                "name, no apostrophe at either end; quote the whole value only)"
            )
        elif not sep:
            reason = 'has no "="'
        elif "=" in name:
            reason = (
                'has more than one "=": entries are separated by ";", and a name '
                'cannot contain "="'
            )
        elif not vin:
            reason = "has an empty VIN"
        elif not name:
            reason = "has an empty name"
        elif vin in mapping:
            reason = f"repeats the VIN of entry {first_seen[vin]}"
        else:
            mapping[vin] = name
            first_seen[vin] = position
            continue
        raise ValueError(f"VEHICLE_MAP entry {position} {reason}. {_VEHICLE_MAP_FORM}")
    return mapping
