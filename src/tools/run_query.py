
import duckdb
import sqlglot
from sqlglot import exp
import threading

DB_PATH = "database/analytics.duckdb"

MAX_ROWS = 500
TIMEOUT_SECONDS = 10


def validate_query(sql):

    statements = sqlglot.parse(
        sql,
        read="duckdb"
    )

    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Apenas uma consulta SQL é permitida.")

    query = statements[0]

    if not isinstance(query, exp.Select):
        raise ValueError("Somente consultas SELECT são permitidas.")

    if query.args.get("into"):
        raise ValueError("SELECT INTO não é permitido.")

    con = duckdb.connect(DB_PATH, read_only=True)

    try:
        available_tables = {
            row[0].lower()
            for row in con.execute("SHOW TABLES").fetchall()
        }
    finally:
        con.close()

    cte_names = {
        cte.alias_or_name.lower()
        for cte in query.find_all(exp.CTE)
    }

    for table in query.find_all(exp.Table):

        table_name = table.name.lower()

        if table_name in cte_names:
            continue

        if table_name not in available_tables:
            raise ValueError(
                f"Tabela não autorizada ou inexistente: {table_name}"
            )

    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter
    )

    if any(
        isinstance(node, forbidden)
        for node in query.walk()
    ):
        raise ValueError("Operação SQL não permitida.")

    limit = query.args.get("limit")

    if limit is None:
        query = query.limit(MAX_ROWS)

    else:
        value = limit.expression

        if not (
            isinstance(value, exp.Literal)
            and value.is_int
        ):
            query = query.limit(MAX_ROWS)

        elif int(value.this) > MAX_ROWS:
            query = query.limit(MAX_ROWS)

    return query.sql(dialect="duckdb")


def run_query(sql):

    try:

        safe_sql = validate_query(sql)

        con = duckdb.connect(
            DB_PATH,
            read_only=True
        )

        timer = threading.Timer(
            TIMEOUT_SECONDS,
            con.interrupt
        )

        timer.daemon = True
        timer.start()

        try:

            cursor = con.execute(safe_sql)

            columns = [
                description[0]
                for description in cursor.description
            ]

            rows = cursor.fetchall()

            return {
                "success": True,
                "columns": columns,
                "rows": [
                    dict(zip(columns, row))
                    for row in rows
                ],
                "row_count": len(rows)
            }

        finally:
            timer.cancel()
            con.close()

    except Exception as error:

        return {
            "success": False,
            "error": str(error)
        }


if __name__ == "__main__":

    result = run_query("""
        SELECT title, price, stars
        FROM vendas
        ORDER BY stars DESC
    """)

    print(result)
