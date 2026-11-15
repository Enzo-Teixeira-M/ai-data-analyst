import duckdb
import json

DB_PATH = "database/analytics.duckdb"
SCHEMA_PATH = "metadata/schema.json"


def get_schema(table_name):

    con = duckdb.connect(DB_PATH, read_only=True)

    columns = con.execute(
        f'DESCRIBE "{table_name}"'
    ).fetchall()

    con.close()

    with open(SCHEMA_PATH, "r", encoding="utf-8") as file:
        metadata = json.load(file)

    table_metadata = metadata["tables"].get(table_name, {})
    descriptions = table_metadata.get("columns", {})

    schema = {
        "table": table_name,
        "description": table_metadata.get("description", ""),
        "columns": []
    }

    for column in columns:
        column_name = column[0]
        column_type = column[1]

        column_metadata = descriptions.get(column_name, {})

        schema["columns"].append({
            "name": column_name,
            "type": column_type,
            "description": column_metadata.get(
                "description",
                "Sem descrição disponível."
            )
        })

    return schema


if __name__ == "__main__":
    result = get_schema("vendas")
    print(json.dumps(result, indent=4, ensure_ascii=False))
