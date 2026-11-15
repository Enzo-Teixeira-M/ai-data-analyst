from pathlib import Path

from src.tools import generate_report as module


def test_generate_report(tmp_path, monkeypatch):

    template_dir = tmp_path / "templates"
    static_dir = tmp_path / "static"
    report_dir = tmp_path / "reports"

    template_dir.mkdir()
    static_dir.mkdir()

    chart_file = static_dir / "chart.umd.min.js"
    chart_file.write_text(
        "CHART_JS_TEST",
        encoding="utf-8"
    )

    template = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Teste de relatório</title>
    </head>
    <body>

        <h1>{{ data.titulo }}</h1>

        <p>{{ data.resumo }}</p>

        <p>{{ generated_at }}</p>

        {% for kpi in data.kpis %}
            <div>
                <strong>{{ kpi.label }}</strong>
                <span>{{ kpi.valor }}</span>
            </div>
        {% endfor %}

        {% for tabela in data.tabelas %}
            <h2>Tabela de resultados</h2>

            {% for linha in tabela.linhas %}
                <p>{{ linha }}</p>
            {% endfor %}
        {% endfor %}

        <pre>{{ sql_formatted }}</pre>

        <p>Etapas SQL: {{ sql_steps|length }}</p>

        <script>{{ chart_js }}</script>

    </body>
    </html>
    """

    (template_dir / "report.html").write_text(
        template,
        encoding="utf-8"
    )

    monkeypatch.setattr(module, "TEMPLATE_DIR", template_dir)
    monkeypatch.setattr(module, "STATIC_DIR", static_dir)
    monkeypatch.setattr(module, "REPORT_DIR", report_dir)

    report_data = {
        "titulo": "Relatório de teste",
        "resumo": "Este é um teste de geração de relatório HTML.",

        "kpis": [
            {
                "label": "Preço médio",
                "valor": 100
            }
        ],

        "graficos": [],

        "tabelas": [
            {
                "colunas": ["price"],
                "linhas": [
                    [100],
                    [200]
                ]
            }
        ],

        "insights": [
            "O relatório foi gerado corretamente."
        ],

        "sql": "SELECT price FROM vendas LIMIT 10",

        "limitacoes": []
    }

    result = module.generate_report(report_data)

    assert result["success"] is True, result.get("error")

    assert "filename" in result
    assert "path" in result
    assert "sql_steps" in result

    assert result["filename"].endswith(".html")

    report_file = Path(result["path"])

    assert report_file.exists()
    assert report_file.is_file()

    assert report_file == report_dir / result["filename"]

    html = report_file.read_text(encoding="utf-8")

    assert "Relatório de teste" in html
    assert "Este é um teste de geração de relatório HTML." in html
    assert "Preço médio" in html
    assert "SELECT" in html
    assert "CHART_JS_TEST" in html

    assert result["sql_steps"] > 0