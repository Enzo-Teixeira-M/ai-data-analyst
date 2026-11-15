
import json
import duckdb

from src.tools import get_schema as module


def test_get_schema(tmp_path, monkeypatch):

    db_path = tmp_path / "test.duckdb"
    metadata_path = tmp_path / "schema.json"

    con = duckdb.connect(str(db_path))

    con.execute("""
        CREATE TABLE vendas (
            id INTEGER,
            price DOUBLE
        )
    """)

    con.execute("""
        INSERT INTO vendas VALUES (1, 100.0)
    """)

    con.close()

    metadata = {
        "tables": {
            "vendas": {
                "description": "Tabela de vendas",
                "columns": {
                    "id": {
                        "description": "Identificador da venda"
                    },
                    "price": {
                        "description": "Preço do produto"
                    }
                }
            }
        }
    }

    metadata_path.write_text(
        json.dumps(metadata),
        encoding="utf-8"
    )

    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "SCHEMA_PATH", str(metadata_path))

    result = module.get_schema()

    assert result[0]["table"] == "vendas"
    assert len(result[0]["columns"]) == 2
    assert len(result[0]["examples"]) == 1
