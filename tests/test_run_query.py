
import duckdb
import pytest

from src.tools import run_query as module


@pytest.fixture
def test_database(tmp_path, monkeypatch):

    db_path = tmp_path / "test.duckdb"

    con = duckdb.connect(str(db_path))

    con.execute("""
        CREATE TABLE vendas (
            id INTEGER,
            price DOUBLE
        )
    """)

    con.execute("""
        INSERT INTO vendas VALUES
        (1, 100.0),
        (2, 200.0)
    """)

    con.close()

    monkeypatch.setattr(
        module,
        "DB_PATH",
        str(db_path)
    )


def test_select_is_allowed(test_database):

    result = module.run_query(
        "SELECT * FROM vendas"
    )

    assert result["success"] is True
    assert result["row_count"] == 2


def test_limit_is_added(test_database):

    sql = module.validate_query(
        "SELECT * FROM vendas"
    )

    assert "LIMIT 500" in sql.upper()


@pytest.mark.parametrize("sql", [
    "DROP TABLE vendas",
    "DELETE FROM vendas",
    "INSERT INTO vendas VALUES (3, 300)"
])
def test_modification_queries_are_rejected(
    test_database,
    sql
):

    with pytest.raises(ValueError):
        module.validate_query(sql)


def test_unknown_table_is_rejected(test_database):

    with pytest.raises(ValueError):
        module.validate_query(
            "SELECT * FROM tabela_inexistente"
        )


def test_multiple_statements_are_rejected(test_database):

    with pytest.raises(ValueError):
        module.validate_query(
            "SELECT * FROM vendas; DROP TABLE vendas"
        )
