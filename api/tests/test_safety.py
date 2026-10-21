from __future__ import annotations

import pytest

from app.runner.compiler import UnsafeSQLError, assert_readonly


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select count(*) from t  ",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SHOW TABLES",
        "DESCRIBE t",
        "PRAGMA table_info('t')",
    ],
)
def test_readonly_allows_selects(sql: str) -> None:
    assert_readonly(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE users",
        "SELECT 1; DELETE FROM t",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "  ; DELETE FROM users",
        "EXEC sp_who",
    ],
)
def test_readonly_blocks_mutations(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        assert_readonly(sql)
