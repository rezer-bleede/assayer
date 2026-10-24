from __future__ import annotations

from app.schemas import ConnectionSpec, EtlSubmit


def test_secret_round_trips_through_json_dump() -> None:
    """Regression: SecretStr serialises to '**********', which breaks persistence."""
    spec = ConnectionSpec(
        type="postgres", name="dwh", host="db", database="assayer", password="s3cr3t"
    )
    dumped = spec.model_dump(mode="json")
    assert dumped["password"] == "s3cr3t", "secret must survive a JSON round-trip"

    restored = ConnectionSpec.model_validate(dumped)
    assert restored.password.get_secret_value() == "s3cr3t"


def test_redacted_never_leaks_the_secret() -> None:
    spec = ConnectionSpec(
        type="snowflake",
        name="wh",
        token="tok_live_abc",
        credentials_json='{"key":"abc"}',
        password="pw",
    )
    red = spec.redacted()
    assert red["password"] == "***"
    assert red["token"] == "***"
    assert red["credentials_json"] == "***"
    assert "s3cr3t" not in str(red)


def test_none_secrets_stay_none() -> None:
    dumped = ConnectionSpec(type="sqlite", name="f").model_dump(mode="json")
    assert dumped["password"] is None
    assert dumped["token"] is None


def test_table_ref_alias_accepts_schema_keyword() -> None:
    from app.schemas import TableRef

    ref = TableRef.model_validate({"source_id": "c1", "schema": "analytics", "name": "rev"})
    assert ref.schema_ == "analytics"
    assert ref.fqn == "analytics.rev"
    # FastAPI serialises responses with by_alias=True -> the wire shape is "schema"
    assert ref.model_dump(by_alias=True)["schema"] == "analytics"
    # ...and internal persistence round-trips through the python-mode name
    assert TableRef.model_validate(ref.model_dump(mode="json")).schema_ == "analytics"


def test_etl_submit_accepts_files_and_manifest() -> None:
    s = EtlSubmit(language="dbt", files={"models/a.sql": "select 1"}, manifest={"nodes": {}})
    assert s.files and s.manifest == {"nodes": {}}


def test_table_ref_serializes_as_schema_without_by_alias() -> None:
    """The UI reads `table.schema`; the API must emit it in every dump mode."""
    from app.schemas import TableRef

    ref = TableRef(source_id="c1", schema="analytics", name="rev")
    assert ref.model_dump(mode="json")["schema"] == "analytics"
    assert "schema_" not in ref.model_dump(mode="json")
    # round-trips back from the wire shape
    assert TableRef.model_validate(ref.model_dump(mode="json")).schema_ == "analytics"
