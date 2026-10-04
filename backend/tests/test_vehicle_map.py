"""The VEHICLE_MAP setting: VIN -> display name, read from the environment.

``scheduler.sh`` passes no vehicle names on the command line; the pipeline CLI
reads the map from the settings (environment / .env) unless ``--vehicle-map``
flags are given. Every VIN here is a placeholder.
"""

from __future__ import annotations

import pytest

from chargewise.config import Settings, parse_vehicle_map
from chargewise.ingest import pipeline

VIN1 = "TESTVIN0000000001"
VIN2 = "TESTVIN0000000002"
BOM = "﻿"


# --------------------------------------------------------------------------- #
# Parsing.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "   ", ";", " ; ; ", '""', "''", BOM])
def test_empty_value_is_an_empty_map(value: str) -> None:
    assert parse_vehicle_map(value) == {}


def test_one_entry() -> None:
    assert parse_vehicle_map(f"{VIN1}=Tesla 2") == {VIN1: "Tesla 2"}


def test_several_entries_keep_their_order() -> None:
    parsed = parse_vehicle_map(f"{VIN1}=Tesla 1;{VIN2}=Tesla 2")
    assert parsed == {VIN1: "Tesla 1", VIN2: "Tesla 2"}
    assert list(parsed) == [VIN1, VIN2]


def test_names_may_contain_spaces_commas_and_apostrophes() -> None:
    parsed = parse_vehicle_map(f"{VIN1}=Pool Car North;{VIN2}=Pool car, the depot's van")
    assert parsed == {VIN1: "Pool Car North", VIN2: "Pool car, the depot's van"}
    # Inside quotes round the whole value too.
    quoted = parse_vehicle_map(f'"{VIN1}=Rock \'n\' roll;{VIN2}=The depot\'s van"')
    assert quoted == {VIN1: "Rock 'n' roll", VIN2: "The depot's van"}


def test_whitespace_and_a_trailing_separator_are_ignored() -> None:
    parsed = parse_vehicle_map(f"  {VIN1} =  Tesla 1  ;\n {VIN2}=Tesla 2 ; ")
    assert parsed == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quotes_left_around_the_whole_value_are_dropped(quote: str) -> None:
    # Some env-file readers hand the value over with its quotes still on.
    parsed = parse_vehicle_map(f"{quote}{VIN1}=Tesla 1;{VIN2}=Tesla 2{quote}")
    assert parsed == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


def test_vin_keys_are_upper_cased() -> None:
    parsed = parse_vehicle_map(f"{VIN1.lower()}=Tesla 1;{VIN2.title()}=Tesla 2")
    assert parsed == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


@pytest.mark.parametrize(
    "value",
    [
        f"{BOM}{VIN1}=Tesla 1;{VIN2}=Tesla 2",        # file saved with a byte-order mark
        f'"{BOM}{VIN1}=Tesla 1;{VIN2}=Tesla 2"',
        f" {BOM} {VIN1}=Tesla 1;{BOM}{VIN2}=Tesla 2",
    ],
)
def test_a_leading_byte_order_mark_is_ignored(value: str) -> None:
    assert parse_vehicle_map(value) == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


ONE_END = "VEHICLE_MAP has a quote mark at one end only"
NOT_ALLOWED = "has a quote mark where none is allowed"
TWO_EQUALS = 'has more than one "=": entries are separated by ";", and a name cannot contain "="'

REJECTED = [
    # (value, start of the message)
    (f"{VIN1}", 'VEHICLE_MAP entry 1 has no "="'),
    (f"{VIN1}=Tesla 1;{VIN2}", 'VEHICLE_MAP entry 2 has no "="'),
    (f"{VIN1}=Tesla 1;;{VIN2}", 'VEHICLE_MAP entry 2 has no "="'),      # blanks are not counted
    # A wrong separator would otherwise fold two cars into one name.
    (f"{VIN1}=Tesla 1,{VIN2}=Tesla 2", f"VEHICLE_MAP entry 1 {TWO_EQUALS}"),
    (f"{VIN1}=Tesla 1 {VIN2}=Tesla 2", f"VEHICLE_MAP entry 1 {TWO_EQUALS}"),
    (f"{VIN1}=Tesla 1;{VIN2}=A=B", f"VEHICLE_MAP entry 2 {TWO_EQUALS}"),   # "=" in a name
    (f"{VIN1}=Tesla 1;=Tesla 2", "VEHICLE_MAP entry 2 has an empty VIN"),
    (f"{VIN1}=Tesla 1; {BOM} =Tesla 2", "VEHICLE_MAP entry 2 has an empty VIN"),
    (f"{VIN1}=;{VIN2}=Tesla 2", "VEHICLE_MAP entry 1 has an empty name"),
    (f"{VIN1}=Tesla 1;{VIN1}=Tesla 2", "VEHICLE_MAP entry 2 repeats the VIN of entry 1"),
    (f"{VIN1}=Tesla 1;{VIN1.lower()}=Tesla 2", "VEHICLE_MAP entry 2 repeats the VIN of entry 1"),
    # Quotes: one pair around the whole value, or none.
    (f'"{VIN1}=Tesla 1;{VIN2}=Tesla 2', ONE_END),
    (f"{VIN1}=Tesla 1;{VIN2}=Tesla 2'", ONE_END),
    (f"\"{VIN1}=Tesla 1;{VIN2}=Tesla 2'", ONE_END),
    ('"', ONE_END),
    (f'"{VIN1}=Tesla 1";"{VIN2}=Tesla 2"', f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
    (f"'{VIN1}=Tesla 1';'{VIN2}=Tesla 2'", f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
    (f"{VIN1}=Tesla 1;'{VIN2}'=Tesla 2;", f"VEHICLE_MAP entry 2 {NOT_ALLOWED}"),
    (f'{VIN1}=Tesla 1;{VIN2}="Tesla 2";', f"VEHICLE_MAP entry 2 {NOT_ALLOWED}"),
    # Names: no double quote anywhere, no apostrophe at either end.
    (f'{VIN1}=Tesla "one" car;{VIN2}=Tesla 2', f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
    (f"{VIN1}='Tis the first;{VIN2}=Tesla 2", f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
    (f"{VIN1}=The drivers';{VIN2}=Tesla 2", f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
    # ...and when that name is the last entry, the apostrophe is the value's own end.
    (f"{VIN1}=Tesla 1;{VIN2}=The drivers'", ONE_END),
]


def test_quote_messages_state_the_rule_for_names() -> None:
    """Whichever message a stray apostrophe produces, it says what a name may not do."""
    with pytest.raises(ValueError) as at_the_end:
        parse_vehicle_map(f"{VIN1}=Tesla 1;{VIN2}=The drivers'")
    assert "a name cannot start or end with an apostrophe" in str(at_the_end.value)
    assert "Put one pair of quotes round the whole value, or none" in str(at_the_end.value)

    with pytest.raises(ValueError) as in_the_middle:
        parse_vehicle_map(f"{VIN1}=The drivers';{VIN2}=Tesla 2")
    assert "no apostrophe at either end" in str(in_the_middle.value)
    assert "no double quote in a VIN or name" in str(in_the_middle.value)


@pytest.mark.parametrize(("value", "message"), REJECTED)
def test_refusal_message_fits_the_status_field_whole(value: str, message: str) -> None:
    """``/api/status`` shows at most MAX_ERROR_CHARS; the message must not be cut."""
    with pytest.raises(ValueError) as excinfo:
        parse_vehicle_map(value)
    shown = f"ChargeWise ingestion not started: {excinfo.value}"
    assert len(shown) <= pipeline.MAX_ERROR_CHARS


@pytest.mark.parametrize(("value", "message"), REJECTED)
def test_malformed_value_is_rejected_with_the_entry_number_and_reason(
    value: str, message: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_vehicle_map(value)
    text = str(excinfo.value)
    assert text.startswith(message)
    assert 'Expected VEHICLE_MAP="VIN1=Name 1;VIN2=Name 2".' in text   # says what is expected


@pytest.mark.parametrize(("value", "message"), REJECTED)
def test_error_message_never_repeats_a_vin_or_the_entry(value: str, message: str) -> None:
    """The message reaches a container log: no VIN and no entry text in it."""
    with pytest.raises(ValueError) as excinfo:
        parse_vehicle_map(value)
    text = str(excinfo.value).upper()
    assert VIN1 not in text and VIN2 not in text
    assert "TESLA" not in text and "TESTVIN" not in text


# --------------------------------------------------------------------------- #
# The setting itself: environment variable and .env file.
# --------------------------------------------------------------------------- #

def test_setting_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEHICLE_MAP", raising=False)
    assert Settings(_env_file=None).vehicle_map == ""


def test_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEHICLE_MAP", f"{VIN1}=Tesla 1;{VIN2}=Tesla 2")
    settings = Settings(_env_file=None)
    assert parse_vehicle_map(settings.vehicle_map) == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


@pytest.mark.parametrize("quote", ['"', ""])
def test_setting_is_read_from_a_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path, quote: str
) -> None:
    """The documented .env line works quoted (as in .env.example) and unquoted."""
    monkeypatch.delenv("VEHICLE_MAP", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TESLAFI_TOKEN=dummy\nVEHICLE_MAP={quote}{VIN1}=Tesla 1;{VIN2}=Tesla 2{quote}\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert parse_vehicle_map(settings.vehicle_map) == {VIN1: "Tesla 1", VIN2: "Tesla 2"}


def test_dotenv_file_saved_with_a_byte_order_mark(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """VEHICLE_MAP on the first line of a .env that starts with a BOM is still read."""
    monkeypatch.delenv("VEHICLE_MAP", raising=False)
    monkeypatch.delenv("TESLAFI_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbf" + f'VEHICLE_MAP="{VIN1}=Tesla 1;{VIN2}=Tesla 2"\nTESLAFI_TOKEN=dummy\n'.encode()
    )
    settings = Settings(_env_file=env_file)
    assert parse_vehicle_map(settings.vehicle_map) == {VIN1: "Tesla 1", VIN2: "Tesla 2"}
    assert settings.teslafi_token == "dummy"

    # The same file without the mark reads the same.
    env_file.write_bytes(env_file.read_bytes()[3:])
    assert Settings(_env_file=env_file).vehicle_map == settings.vehicle_map


# --------------------------------------------------------------------------- #
# Precedence in the pipeline CLI: --vehicle-map flags win over the setting.
# --------------------------------------------------------------------------- #

@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Run ``pipeline.main`` with a given VEHICLE_MAP; return run_pipeline's kwargs.

    The settings point at a database in ``tmp_path``: a refused run records
    itself there.
    """

    def run(argv: list[str], setting: str = "") -> dict[str, object]:
        seen: dict[str, object] = {}

        async def fake_run_pipeline(**kwargs: object) -> dict[str, object]:
            seen.update(kwargs)
            return {}

        monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(
            pipeline, "get_settings",
            lambda: Settings(
                _env_file=None, vehicle_map=setting,
                database_url=f"sqlite:///{tmp_path / 'cw.sqlite'}",
            ),
        )
        pipeline.main(argv)
        return seen

    return run


def test_cli_uses_the_setting_when_no_flag_is_given(cli) -> None:
    seen = cli(["--teslafi"], setting=f"{VIN1}=Tesla 1;{VIN2}=Tesla 2")
    assert seen["vehicle_map"] == {VIN1: "Tesla 1", VIN2: "Tesla 2"}
    assert seen["teslafi"] is True


def test_cli_flags_win_over_the_setting(cli) -> None:
    seen = cli(
        ["--teslafi", "--vehicle-map", f"{VIN2}=From the flag"],
        setting=f"{VIN1}=Tesla 1;{VIN2}=Tesla 2",
    )
    # The flags replace the setting altogether: VIN1's entry is not merged in.
    assert seen["vehicle_map"] == {VIN2: "From the flag"}


def test_cli_flags_are_not_checked_against_a_malformed_setting(cli) -> None:
    seen = cli(["--teslafi", "--vehicle-map", f"{VIN1}=Tesla 1"], setting="not a map")
    assert seen["vehicle_map"] == {VIN1: "Tesla 1"}


def test_cli_without_flag_or_setting_passes_no_map(cli) -> None:
    assert cli(["--teslafi"], setting="")["vehicle_map"] is None


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        (f"{VIN1}=Tesla 1;{VIN2}", 'VEHICLE_MAP entry 2 has no "="'),
        (f"{VIN1}=Tesla 1,{VIN2}=Tesla 2", 'VEHICLE_MAP entry 1 has more than one "="'),
        (f'"{VIN1}=Tesla 1";"{VIN2}=Tesla 2"', f"VEHICLE_MAP entry 1 {NOT_ALLOWED}"),
        (f'"{VIN1}=Tesla 1;{VIN2}=Tesla 2', ONE_END),
        (f"{VIN1}=Tesla 1;=Tesla 2", "VEHICLE_MAP entry 2 has an empty VIN"),
        (f"{VIN1}=;{VIN2}=Tesla 2", "VEHICLE_MAP entry 1 has an empty name"),
    ],
)
def test_cli_rejects_a_malformed_setting_before_anything_runs(
    cli, capsys: pytest.CaptureFixture[str], setting: str, reason: str
) -> None:
    """Exit status 1 with a one-line message, and run_pipeline is never called."""
    called: dict[str, object] = {}
    with pytest.raises(SystemExit) as excinfo:
        called = cli(["--teslafi"], setting=setting)
    message = str(excinfo.value.code)
    assert message.startswith(f"ChargeWise ingestion not started: {reason}")
    assert VIN1 not in message.upper() and VIN2 not in message.upper()
    assert called == {}                                   # nothing ran, so no network call
    assert "ingestion complete" not in capsys.readouterr().out


def test_parsed_map_names_vehicles() -> None:
    """The parsed setting names vehicles just as ``--vehicle-map`` flags do."""
    mapping = parse_vehicle_map(f"{VIN1}=Tesla 1;{VIN2}=Tesla 2")
    assert pipeline.vehicle_name_for(VIN1, "modely", mapping) == "Tesla 1"
    assert pipeline.vehicle_name_for(VIN2, "modely", mapping) == "Tesla 2"
    # A VIN the map does not name gets the model-and-suffix default.
    assert pipeline.vehicle_name_for("TESTVIN0000000003", "modely", mapping) == "Modely (000003)"
