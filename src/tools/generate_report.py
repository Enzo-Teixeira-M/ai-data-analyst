import json
from pathlib import Path
from datetime import datetime

import sqlglot
from sqlglot import exp
from jinja2 import Environment, FileSystemLoader, select_autoescape


BASE_DIR = Path(__file__).resolve().parents[2]

TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
REPORT_DIR = BASE_DIR / "reports"


def validate_report(data):

    if isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, dict):
        raise ValueError("O relatório precisa ser um objeto JSON.")

    defaults = {
        "titulo": "",
        "resumo": "",
        "kpis": [],
        "graficos": [],
        "tabelas": [],
        "insights": [],
        "sql": "",
        "limitacoes": []
    }

    for key, value in defaults.items():
        data.setdefault(key, value)

    for key in ("titulo", "resumo", "sql"):
        if not isinstance(data[key], str):
            raise ValueError(f"{key} precisa ser texto.")

    for key in (
        "kpis",
        "graficos",
        "tabelas",
        "insights",
        "limitacoes"
    ):
        if not isinstance(data[key], list):
            raise ValueError(f"{key} precisa ser uma lista.")

    for kpi in data["kpis"]:

        if not isinstance(kpi, dict):
            raise ValueError("KPI inválido.")

        if not isinstance(kpi.get("label"), str):
            raise ValueError("KPI inválido: informe label.")

        if not isinstance(kpi.get("valor"), (str, int, float)):
            raise ValueError("Valor de KPI inválido.")

    for chart in data["graficos"]:

        if not isinstance(chart, dict):
            raise ValueError("Gráfico inválido.")

        if chart.get("tipo") not in ("bar", "line", "pie"):
            raise ValueError(
                "Gráfico inválido. Tipos aceitos: bar, line e pie."
            )

        if not isinstance(chart.get("titulo"), str):
            raise ValueError("Título de gráfico inválido.")

        if not isinstance(chart.get("x"), list):
            raise ValueError("O eixo x precisa ser uma lista.")

        if not isinstance(chart.get("y"), list):
            raise ValueError("O eixo y precisa ser uma lista.")

        if len(chart["x"]) != len(chart["y"]):
            raise ValueError(
                "Os eixos x e y precisam ter o mesmo tamanho."
            )

        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in chart["y"]
        ):
            raise ValueError(
                "Os valores do eixo y precisam ser numéricos."
            )

    for table in data["tabelas"]:

        if not isinstance(table, dict):
            raise ValueError("Tabela inválida.")

        cols = table.get("colunas", [])
        rows = table.get("linhas", [])

        if not isinstance(cols, list):
            raise ValueError("Colunas precisam ser uma lista.")

        if not all(isinstance(col, str) for col in cols):
            raise ValueError("Colunas precisam ser textos.")

        if not isinstance(rows, list):
            raise ValueError("Linhas precisam ser uma lista.")

        for row in rows:

            if not isinstance(row, list):
                raise ValueError("Cada linha precisa ser uma lista.")

            if len(row) != len(cols):
                raise ValueError(
                    "Cada linha deve corresponder às colunas."
                )

    if not all(
        isinstance(value, str)
        for value in data["insights"] + data["limitacoes"]
    ):
        raise ValueError(
            "Insights e limitações precisam ser textos."
        )

    return data


def _sql_text(node):
    return node.sql(dialect="duckdb") if node is not None else ""


def explain_sql(sql):

    if not sql or not sql.strip():

        return "", [
            {
                "title": "SQL não informado",
                "description": (
                    "Não foi registrada uma consulta para este relatório."
                )
            }
        ]

    try:

        parsed = sqlglot.parse(sql, read="duckdb")

        if len(parsed) != 1 or parsed[0] is None:
            raise ValueError(
                "Era esperada exatamente uma instrução SQL."
            )

        query = parsed[0]

        formatted = query.sql(
            dialect="duckdb",
            pretty=True
        )

        select = query if isinstance(query, exp.Select) else None

        if (
            select is None
            and isinstance(query, exp.Subquery)
            and isinstance(query.this, exp.Select)
        ):
            select = query.this

        if select is None:

            return formatted, [
                {
                    "title": "Instrução SQL",
                    "description": (
                        "A consulta foi registrada. Consulte o SQL "
                        "completo abaixo para verificar a instrução executada."
                    )
                }
            ]

        steps = []

        if select.expressions:

            selected_fields = ", ".join(
                _sql_text(expression)
                for expression in select.expressions
            )

            steps.append({
                "title": "SELECT — campos e cálculos",
                "description": (
                    "Seleciona: " + selected_fields + "."
                )
            })

        clauses = [
            (
                "from_",
                "FROM — origem dos dados",
                "Os registros são obtidos de: "
            ),
            (
                "where",
                "WHERE — filtros",
                "Aplica a condição: "
            ),
            (
                "group",
                "GROUP BY — agrupamento",
                "Agrupa os registros por: "
            ),
            (
                "having",
                "HAVING — filtro dos grupos",
                "Filtra os grupos pela condição: "
            ),
            (
                "order",
                "ORDER BY — ordenação",
                "Ordena os resultados por: "
            ),
            (
                "limit",
                "LIMIT — limite de resultados",
                "Limita os resultados usando: "
            ),
            (
                "offset",
                "OFFSET — deslocamento",
                "Desloca o início dos resultados usando: "
            )
        ]

        for key, title, description in clauses:

            node = select.args.get(key)

            if node is None:
                continue

            value = (
                node.this
                if key in ("where", "having")
                else node
            )

            if key in ("group", "order"):

                value = ", ".join(
                    _sql_text(expression)
                    for expression in node.expressions
                )

            else:

                value = _sql_text(value)

            steps.append({
                "title": title,
                "description": description + str(value) + "."
            })

        for join in select.args.get("joins") or []:

            steps.append({
                "title": "JOIN — combinação de dados",
                "description": (
                    "Combina fontes de dados usando: "
                    + _sql_text(join)
                    + "."
                )
            })

        if not steps:

            steps.append({
                "title": "Consulta analisada",
                "description": (
                    "A consulta foi interpretada, mas não foram "
                    "encontradas cláusulas para detalhar."
                )
            })

        return formatted, steps

    except Exception as error:

        return sql.strip(), [
            {
                "title": "SQL preservado",
                "description": (
                    "A decomposição automática não foi concluída. "
                    "O SQL original está disponível abaixo. "
                    f"Detalhe: {error}"
                )
            }
        ]


def generate_report(report_data):

    try:

        data = validate_report(report_data)

        env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "xml"])
        )

        template = env.get_template("report.html")

        chart_path = STATIC_DIR / "chart.umd.min.js"

        if not chart_path.exists():

            raise FileNotFoundError(
                f"Chart.js não encontrado: {chart_path}"
            )

        chart_js = chart_path.read_text(
            encoding="utf-8"
        )

        sql_formatted, sql_steps = explain_sql(data["sql"])

        now = datetime.now()

        filename = (
            "report_"
            + now.strftime("%Y%m%d_%H%M%S_%f")
            + ".html"
        )

        html = template.render(
            data=data,
            generated_at=now.strftime("%d/%m/%Y %H:%M:%S"),
            chart_js=chart_js,
            sql_formatted=sql_formatted,
            sql_steps=sql_steps
        )

        REPORT_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        path = REPORT_DIR / filename

        path.write_text(
            html,
            encoding="utf-8"
        )

        return {
            "success": True,
            "path": str(path),
            "filename": filename,
            "sql_steps": len(sql_steps)
        }

    except Exception as error:

        return {
            "success": False,
            "error": str(error)
        }