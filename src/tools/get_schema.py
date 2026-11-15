
import duckdb
import json

DB_PATH = "database/analytics.duckdb"
SCHEMA_PATH = "metadata/schema.json"


def get_schema():

    con = duckdb.connect(DB_PATH, read_only=True)

    tables = con.execute("SHOW TABLES").fetchall()

    with open(SCHEMA_PATH, "r", encoding="utf-8") as file:
        metadata = json.load(file)

    result = []

    for (table_name,) in tables:

        columns = con.execute(
            f'DESCRIBE "{table_name}"'
        ).fetchall()

        table_metadata = metadata["tables"].get(
            table_name, {}
        )

        descriptions = table_metadata.get("columns", {})

        column_data = []

        for column in columns:

            name = column[0]
            data_type = column[1]

            description = descriptions.get(name, {}).get(
                "description",
                "Sem descrição disponível."
            )

            column_data.append({
                "name": name,
                "type": data_type,
                "description": description
            })

        quoted_name = table_name.replace('"', '""')

        rows = con.execute(
            f'SELECT * FROM "{quoted_name}" LIMIT 3'
        ).fetchall()

        examples = [
            dict(zip(
                [column[0] for column in columns],
                row
            ))
            for row in rows
        ]

        result.append({
            "table": table_name,
            "description": table_metadata.get(
                "description", ""
            ),
            "columns": column_data,
            "examples": examples
        })

    con.close()

    return result


if __name__ == "__main__":
    print(json.dumps(
        get_schema(),
        indent=4,
        ensure_ascii=False,
        default=str
    ))
