"""Tests for repo fetch URL validation and credential scrubbing."""

from __future__ import annotations

from app.services.repo_fetch import _refuse, scrub, validate_repo_url


def test_validate_repo_url_https_only() -> None:
    for bad in [
        "git://github.com/acme/repo.git",
        "ssh://git@github.com/acme/repo.git",
        "/home/user/repo",
        "github.com/acme/repo",
        "",
    ]:
        try:
            validate_repo_url(bad)
        except ValueError:
            continue
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_validate_repo_url_credentials_stripped() -> None:
    with_url = "postgresql://user:p4ssw0rd@db.example.com:5432/mydb"
    try:
        validate_repo_url(with_url)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for URL with credentials")


def test_validate_repo_url_blocks_private_metadata() -> None:
    # localhost / loopback / link-local / metadata should all be refused
    for bad in [
        "http://localhost:5432/repo",
        "http://127.0.0.1:80/repo",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:80/repo",
    ]:
        try:
            validate_repo_url(bad)
        except ValueError:
            continue
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_validate_repo_url_allows_https() -> None:
    url = validate_repo_url("https://github.com/acme/analytics.git")
    assert url == "https://github.com/acme/analytics.git"
    url = validate_repo_url("https://github.com:443/acme/analytics.git")
    assert "github.com" in url


def test_validate_repo_url_strips_trailing_slash() -> None:
    assert validate_repo_url("https://github.com/acme/repo/") == "https://github.com/acme/repo"


def test_scrub_removes_credentials() -> None:
    assert "p4ssw0rd" not in scrub("postgresql://user:p4ssw0rd@db.example.com/db")
    assert scrub("postgresql://user:p4ssw0rd@db.example.com/db") == (
        "postgresql://***@***@db.example.com/db"
    )
    assert scrub("no credentials here") == "no credentials here"
    assert scrub("") == ""


def test_scrub_truncates_long_messages() -> None:
    long_msg = "x" * 1000
    assert len(scrub(long_msg)) <= 500
    assert scrub(long_msg).endswith("...") is False  # truncated at 500 chars exactly


def test_refuse_raises_valueerror() -> None:
    try:
        _refuse("something went wrong")
    except ValueError as exc:
        assert str(exc) == "something went wrong"
    else:
        raise AssertionError("expected ValueError")
