"""
IA Analista de Dados — pipeline de análise em etapas.

Em vez de deixar um modelo pequeno decidir sozinho quando consultar, parar e
reportar, o Python conduz o processo e usa o modelo onde ele é mais útil:

  1. PERFIL      Python calcula o perfil real da tabela (nulos, zeros, distribuições).
  2. PLANO       Modelo interpreta a pergunta e planeja 3-5 consultas complementares.
  3. EXECUÇÃO    Consultas validadas (somente SELECT), com auto-correção de SQL.
  4. EVIDÊNCIAS  Python calcula fatos estatísticos (concentração, outliers,
                 correlações, média vs mediana) sobre cada resultado.
  5. APROFUNDA   Modelo critica a evidência e pede consultas complementares.
  6. SÍNTESE     Modelo escreve a análise (JSON estruturado); gráficos, tabelas e
                 KPIs são montados pelo Python a partir dos dados reais.
  7. VALIDAÇÃO   Números citados nos textos são conferidos contra os resultados.
  8. RELATÓRIO   generate_report + resposta de chat que complementa o dashboard.
"""

import itertools
import json
import math
import numbers
import os
import re
import statistics
import time
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import ollama
import sqlglot
from sqlglot import exp

from src.tools.get_schema import get_schema
from src.tools.run_query import run_query
from src.tools.generate_report import generate_report

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
MODELO = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))  # o padrão do Ollama (2048-4096) trunca o esquema

TABLE_NAME = "vendas"
ALLOWED_TABLES = {TABLE_NAME}

MAX_SQL_ATTEMPTS = 14         # execuções totais (inclui tentativas de correção)
MAX_SQL_REPAIRS = 2           # correções por consulta
MAX_PLAN_QUERIES = 5
MAX_DEEPENING_ROUNDS = 2
MAX_FOLLOWUPS_PER_ROUND = 2
DEFAULT_LIMIT = 50            # LIMIT acrescentado quando o modelo esquece
MAX_LIMIT = 500
DIGEST_CHARS = 9000           # tamanho máximo do resumo de evidências enviado ao modelo
HISTORY_MESSAGES = 8
HISTORY_CHARS = 1200
PROFILE_TTL_SECONDS = 600

# Tipos de gráfico que o seu generate_report sabe desenhar. O fallback original
# só usava "bar"; acrescente "line", "pie" etc. aqui se o seu gerador suportar.
SUPPORTED_CHART_TYPES = ("bar",)

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "agent.jsonl"

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
PERSONA = """
Você é um analista de dados e cientista de dados sênior, com forte senso de negócio. Responda sempre em português do Brasil, em linguagem clara para quem toma decisões.

PRINCÍPIOS DE ANÁLISE
1. Use exclusivamente os dados retornados pelas consultas e o perfil calculado pelo Python. Nunca invente valores, colunas, estatísticas ou resultados.
2. Cite números exatamente como aparecem nas evidências (sem abreviar para "mil" ou "milhões") e não calcule valores novos de cabeça.
3. Separe FATO (o que os dados mostram), HIPÓTESE (explicação plausível ainda não testada; sempre iniciada por "Hipótese:") e LIMITAÇÃO (o que os dados não permitem concluir).
4. Não afirme causalidade. Correlação indica associação, nada além disso.
5. Nunca confie em médias isoladas: compare com a mediana, considere o tamanho da amostra (n) e outliers.
6. Vá além de descrever números: explique o que eles significam para a decisão (o "e daí?").

SOBRE O DATASET
A tabela vendas representa um catálogo de produtos da Amazon, não transações individuais. Campos conhecidos incluem asin, title, stars, reviews, price, listPrice, categoryName, isBestSeller e boughtInLastMonth. Preços estão em CAD. boughtInLastMonth é um indicador de compras no último mês, não uma série histórica. Assim, é possível comparar categorias, preços, avaliações e compras recentes, mas não afirmar crescimento ou queda ao longo do tempo.
Cuidados de qualidade: valores 0 em price, listPrice e stars podem representar dado ausente (e não preço ou nota zero); confirme com o perfil antes de usá-los em médias. Grupos pequenos geram médias instáveis: ao comparar grupos, use HAVING COUNT(*) >= 30 (ou outro mínimo justificado).
"""

PLAN_INSTRUCTIONS = """
Planeje a investigação como um analista sênior faria.

- interpretacao: reformule a pergunta em termos de métricas e dimensões que existem no dataset.
- tipo_analise: ranking, comparacao, distribuicao, relacao, qualidade ou exploratoria.
- hipoteses: 2 a 4 hipóteses testáveis com SQL.
- consultas: 3 a 5 consultas DuckDB que se complementam:
  (a) a consulta principal, que responde diretamente à pergunta;
  (b) uma de robustez/validação (média vs mediana, n por grupo, % de zeros ou nulos);
  (c) uma que aprofunde ou teste uma hipótese (outra dimensão, faixas de preço, relação entre variáveis com corr()).
  Se a pergunta for simples e factual, 2 ou 3 consultas bastam.

REGRAS DE SQL (DuckDB)
- Somente SELECT, sempre com LIMIT (no máximo 100). Nunca use SELECT *.
- Use apenas a tabela vendas e colunas do esquema. Aliases em português e snake_case (n_produtos, preco_medio, preco_mediano).
- Funções úteis: median(x), quantile_cont(x, 0.9), stddev_samp(x), corr(x, y), count(*) FILTER (WHERE ...), round(x, 2).
- Para médias de preço ou nota, exclua dado ausente (ex.: WHERE price > 0).
"""

REPORT_INSTRUCTIONS = """
Escreva o relatório final em JSON, seguindo o esquema.

- titulo: curto, formulado como conclusão ou pergunta respondida.
- resumo: 3 a 5 frases. A primeira responde diretamente à pergunta; as seguintes trazem 2 ou 3 números-chave e a principal ressalva.
- kpis: 3 a 5 indicadores. Cada um aponta para uma célula de uma evidência (consulta = "C1", coluna = nome exato da coluna, linha = índice começando em 0). Não escreva o valor: o Python o preenche.
- graficos: 2 a 4, preferindo consultas e colunas diferentes. x_coluna é o rótulo (texto) e y_coluna é numérica; use somente colunas que existem. O título deve dizer a mensagem do gráfico.
- tabelas: 1 ou 2, apontando para o id da consulta.
- insights: 4 a 6. Cada um com titulo curto; evidencia (fatos e números das evidências, citando o id); implicacao (o que isso significa para quem decide; hipóteses começam com "Hipótese:"); confianca (alta só se confirmada por mais de uma consulta e com amostra grande; media; baixa); consultas (ids).
- recomendacoes: 3 ou 4 ações concretas, cada uma ligada a um insight.
- limitacoes: 3 a 5 itens específicos desta análise (ex.: LIMIT truncando resultados, ausência de série temporal, zeros como dado ausente, correlação calculada sobre linhas agregadas, preços em CAD).
- proximos_passos: 2 ou 3 análises que aprofundariam a conclusão.
- Use somente números presentes nas evidências, escritos exatamente como lá. Não calcule valores novos. Não use HTML nem markdown.
"""

# --------------------------------------------------------------------------
# Esquemas JSON (saída estruturada do Ollama)
# --------------------------------------------------------------------------
_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}
_QUERY_ITEM = {
    "type": "object",
    "properties": {"objetivo": _STR, "sql": _STR},
    "required": ["objetivo", "sql"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretacao": _STR,
        "tipo_analise": _STR,
        "hipoteses": _STR_LIST,
        "consultas": {"type": "array", "items": _QUERY_ITEM},
    },
    "required": ["interpretacao", "tipo_analise", "hipoteses", "consultas"],
}

SQL_SCHEMA = {"type": "object", "properties": {"sql": _STR}, "required": ["sql"]}

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {"avaliacao": _STR, "consultas": {"type": "array", "items": _QUERY_ITEM}},
    "required": ["avaliacao", "consultas"],
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": _STR,
        "resumo": _STR,
        "kpis": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": _STR, "consulta": _STR, "coluna": _STR, "linha": {"type": "integer"}},
            "required": ["label", "consulta", "coluna", "linha"]}},
        "graficos": {"type": "array", "items": {
            "type": "object",
            "properties": {"titulo": _STR, "tipo": {"type": "string", "enum": list(SUPPORTED_CHART_TYPES)},
                           "consulta": _STR, "x_coluna": _STR, "y_coluna": _STR},
            "required": ["titulo", "tipo", "consulta", "x_coluna", "y_coluna"]}},
        "tabelas": {"type": "array", "items": {
            "type": "object",
            "properties": {"titulo": _STR, "consulta": _STR},
            "required": ["titulo", "consulta"]}},
        "insights": {"type": "array", "items": {
            "type": "object",
            "properties": {"titulo": _STR, "evidencia": _STR, "implicacao": _STR,
                           "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
                           "consultas": _STR_LIST},
            "required": ["titulo", "evidencia", "implicacao", "confianca", "consultas"]}},
        "recomendacoes": _STR_LIST,
        "limitacoes": _STR_LIST,
        "proximos_passos": _STR_LIST,
    },
    "required": ["titulo", "resumo", "kpis", "graficos", "tabelas", "insights",
                 "recomendacoes", "limitacoes", "proximos_passos"],
}

# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def write_log(event, details):
    entry = {"timestamp": datetime.now().isoformat(), "event": event, **details}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def emit(state, message):
    """Mostra o andamento no console e, se houver, para a interface (on_progress)."""
    print(f"[AGENTE] {message}")
    write_log("progress", {"message": message})
    callback = state.get("on_progress")
    if callable(callback):
        try:
            callback(message)
        except Exception:
            pass


def num(v):
    """float se v for número real (int, float, Decimal, numpy); senão None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (numbers.Real, Decimal)):
        try:
            f = float(v)
        except (ValueError, OverflowError):
            return None
        return f if math.isfinite(f) else None
    return None


def fmt(v, dec=2):
    """Formata no padrão brasileiro: 1.234,56."""
    f = num(v)
    if f is None:
        return "—" if v is None else str(v)
    if abs(f - round(f)) < 1e-9 and abs(f) < 1e15:
        return f"{int(round(f)):,}".replace(",", ".")
    return f"{f:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_any(v, dec=2):
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return short(v, 40)
    return fmt(v, dec)


def short(text, n=50):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def qi(name):
    """Identificador SQL entre aspas."""
    return '"' + str(name).replace('"', '""') + '"'


def clean_cell(v):
    if v is None or isinstance(v, (str, bool)):
        return v
    f = num(v)
    if f is None:
        return str(v)
    return int(f) if isinstance(v, numbers.Integral) else round(f, 2)


def round_clean(f):
    return int(f) if float(f).is_integer() else round(f, 2)


def normalize_sql(sql):
    return re.sub(r"\s+", " ", str(sql).strip().rstrip(";")).lower()


def rows_as_dicts(result):
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, (list, tuple)):
            out.append(dict(zip(cols, r)))
    return out


def summarize_result_for_log(result):
    if not isinstance(result, dict):
        return str(result)[:300]
    if not result.get("success"):
        return {"success": False, "error": result.get("error")}
    rows = result.get("rows") or []
    return {"success": True, "columns": result.get("columns"), "row_count": len(rows), "sample": rows[:3]}


# --------------------------------------------------------------------------
# Chamadas ao modelo
# --------------------------------------------------------------------------
def chat_json(messages, schema, temperature=0.2, required=(), retries=1, label="llm"):
    """Chama o Ollama com saída estruturada (JSON Schema) e devolve dict ou None."""
    msgs = list(messages)
    for attempt in range(retries + 1):
        started = time.perf_counter()
        response = ollama.chat(
            model=MODELO,
            messages=msgs,
            format=schema,
            options={"temperature": temperature, "num_ctx": NUM_CTX, "seed": 7},
        )
        content = response.message.content or ""
        write_log("llm_call", {"label": label, "attempt": attempt + 1,
                               "duration_seconds": round(time.perf_counter() - started, 2),
                               "content": content[:4000]})
        try:
            data = json.loads(content)
            if isinstance(data, dict) and all(k in data for k in required):
                return data
        except json.JSONDecodeError:
            pass
        msgs = msgs + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Resposta inválida. Responda apenas com um objeto JSON válido que siga exatamente o esquema pedido."},
        ]
    return None


# --------------------------------------------------------------------------
# Perfil dos dados (calculado pelo Python, sem depender do modelo)
# --------------------------------------------------------------------------
_PROFILE_CACHE = {"ts": 0.0, "text": "", "total_rows": None}
_NUMERIC_PREFIXES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
                     "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")


def _safe_query(sql):
    try:
        res = run_query(sql=sql)
    except Exception as exc:
        write_log("profile_query_error", {"sql": sql, "error": str(exc)})
        return None
    return res if isinstance(res, dict) and res.get("success") is True else None


def build_profile():
    cache = _PROFILE_CACHE
    if cache["text"] and time.time() - cache["ts"] < PROFILE_TTL_SECONDS:
        return cache

    summary = _safe_query(f"SELECT * FROM (SUMMARIZE {TABLE_NAME}) LIMIT 200")
    if not summary:
        return {"text": "", "total_rows": None}

    columns = []
    total = None
    for r in rows_as_dicts(summary):
        ctype = str(r.get("column_type") or "").upper()
        columns.append({"name": r.get("column_name"), "type": ctype, "raw": r,
                        "uniq": num(r.get("approx_unique")),
                        "numeric": ctype.startswith(_NUMERIC_PREFIXES)})
        total = total or num(r.get("count"))

    # % de zeros nas colunas numéricas (em muitos catálogos, 0 significa "sem dado")
    zero_pct = {}
    numeric_cols = [c["name"] for c in columns if c["numeric"]][:8]
    if numeric_cols:
        sel = ", ".join(f"100.0 * COUNT(*) FILTER (WHERE {qi(c)} = 0) / COUNT(*) AS z{i}"
                        for i, c in enumerate(numeric_cols))
        zr = _safe_query(f"SELECT {sel} FROM {TABLE_NAME} LIMIT 1")
        if zr and rows_as_dicts(zr):
            row = rows_as_dicts(zr)[0]
            zero_pct = {c: row.get(f"z{i}") for i, c in enumerate(numeric_cols)}

    lines = [f"Linhas em {TABLE_NAME}: {fmt(total)}" if total else f"Tabela {TABLE_NAME}"]
    for c in columns:
        r, name = c["raw"], c["name"]
        nulls = fmt_any(r.get("null_percentage"))
        if c["numeric"]:
            lines.append(
                f"- {name} ({c['type']}): min {fmt_any(r.get('min'))}, mediana {fmt_any(r.get('q50'))}, "
                f"média {fmt_any(r.get('avg'))}, máx {fmt_any(r.get('max'))}, desvio {fmt_any(r.get('std'))}; "
                f"nulos {nulls}%; zeros {fmt_any(zero_pct.get(name))}%")
        elif c["type"].startswith("VARCHAR") and c["uniq"] and c["uniq"] > 1000:
            lines.append(f"- {name} (texto de alta cardinalidade, ~{fmt(c['uniq'])} valores distintos); nulos {nulls}%")
        else:
            lines.append(f"- {name} ({c['type']}): ~{fmt(c['uniq'])} valores distintos; nulos {nulls}%")

    cats = sorted((c["uniq"], c["name"]) for c in columns
                  if c["type"].startswith(("VARCHAR", "BOOLEAN")) and c["uniq"] and 2 <= c["uniq"] <= 1000)[:3]
    for _, name in cats:
        top = _safe_query(f"SELECT CAST({qi(name)} AS VARCHAR) AS valor, COUNT(*) AS n "
                          f"FROM {TABLE_NAME} GROUP BY 1 ORDER BY n DESC LIMIT 8")
        if top:
            items = "; ".join(f"{short(d.get('valor'), 40)} ({fmt(d.get('n'))})" for d in rows_as_dicts(top))
            lines.append(f"- Valores mais frequentes de {name}: {items}")

    cache.update({"ts": time.time(), "text": "\n".join(lines), "total_rows": int(total) if total else None})
    return cache


# --------------------------------------------------------------------------
# Execução segura de SQL
# --------------------------------------------------------------------------
def execute_query(sql, state, objetivo=""):
    if state["sql_attempts"] >= MAX_SQL_ATTEMPTS:
        return {"success": False, "error": f"Limite de {MAX_SQL_ATTEMPTS} consultas atingido."}
    if not isinstance(sql, str) or not sql.strip():
        return {"success": False, "error": "SQL vazio."}
    sql = sql.strip().rstrip(";").strip()

    try:
        stmts = [p for p in sqlglot.parse(sql, read="duckdb") if p is not None]
    except Exception as exc:
        return {"success": False, "error": f"SQL inválido: {exc}"}
    if len(stmts) != 1 or not isinstance(stmts[0], (exp.Select, exp.Union)):
        return {"success": False, "error": "Somente uma instrução SELECT é permitida por chamada."}
    stmt = stmts[0]

    # Defesa adicional: só tabelas permitidas (bloqueia read_csv(), arquivos, information_schema...)
    ctes = {(c.alias or "").lower() for c in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        name = (table.name or "").lower()
        if name not in ALLOWED_TABLES and name not in ctes:
            return {"success": False,
                    "error": f"Tabela não permitida: {table.sql()[:60]}. Use somente: {', '.join(sorted(ALLOWED_TABLES))}."}

    limit = stmt.args.get("limit")
    if limit is None:
        sql = f"{sql}\nLIMIT {DEFAULT_LIMIT}"
    else:
        try:
            if int(limit.expression.name) > MAX_LIMIT:
                return {"success": False, "error": f"LIMIT máximo é {MAX_LIMIT}."}
        except (AttributeError, ValueError, TypeError):
            pass

    state["sql_attempts"] += 1
    started = time.perf_counter()
    try:
        result = run_query(sql=sql)
    except Exception as exc:
        write_log("query_error", {"sql": sql, "error": str(exc)})
        return {"success": False, "error": str(exc)}

    ok = isinstance(result, dict) and result.get("success") is True
    write_log("query_execution", {"sql": sql, "objetivo": objetivo, "summary": summarize_result_for_log(result),
                                  "duration_seconds": round(time.perf_counter() - started, 4)})
    if not ok:
        err = result.get("error") if isinstance(result, dict) else str(result)
        return {"success": False, "error": str(err or "erro desconhecido")}
    result = dict(result)
    result["sql"] = sql  # SQL efetivamente executado (pode ter ganho um LIMIT)
    return result


def repair_sql(sql, error, system):
    prompt = (
        "A consulta abaixo falhou no DuckDB. Corrija-a mantendo o objetivo.\n\n"
        f"SQL:\n{sql}\n\nERRO:\n{error}\n\n"
        f"Use somente a tabela {TABLE_NAME} e colunas do esquema, mantenha LIMIT e devolva apenas o JSON com o campo sql."
    )
    data = chat_json([system, {"role": "user", "content": prompt}], SQL_SCHEMA,
                     temperature=0.0, required=("sql",), retries=0, label="repair_sql")
    fixed = data.get("sql") if data else None
    return fixed if isinstance(fixed, str) and fixed.strip() else None


def run_with_repair(item, state, system):
    """Executa uma consulta do plano; se falhar, pede ao modelo uma correção (até MAX_SQL_REPAIRS)."""
    sql, objetivo = item["sql"], item.get("objetivo") or "Consulta de apoio"
    key = normalize_sql(sql)
    if key in state["seen_sql"]:
        return None
    state["seen_sql"].add(key)

    for attempt in range(MAX_SQL_REPAIRS + 1):
        result = execute_query(sql, state, objetivo)
        if result.get("success"):
            ev = register_evidence(state, objetivo, result["sql"], result)
            emit(state, f"{ev['id']} concluída: {objetivo} ({len(ev['rows'])} linhas)")
            return ev
        error = result.get("error", "erro desconhecido")
        emit(state, f"Consulta falhou: {short(error, 120)}")
        if attempt == MAX_SQL_REPAIRS or state["sql_attempts"] >= MAX_SQL_ATTEMPTS:
            break
        fixed = repair_sql(sql, error, system)
        if not fixed or normalize_sql(fixed) == normalize_sql(sql):
            break
        sql = fixed
        state["seen_sql"].add(normalize_sql(sql))
    return None


# --------------------------------------------------------------------------
# Evidências: estatística calculada pelo Python sobre cada resultado
# --------------------------------------------------------------------------
_NON_ADDITIVE = re.compile(r"(avg|media|média|mean|median|mediana|pct|perc|taxa|ratio|razao|razão|"
                           r"desvio|std|corr|min|max|nota|stars|preco|preço|price)", re.I)
_ADDITIVE = re.compile(r"(total|soma|sum|qtd|quantidade|count|contagem|^n$|^n_|_n$|num_|produtos|"
                       r"compras|vendas|receita|reviews|avalia)", re.I)


def is_additive(col):
    return not _NON_ADDITIVE.search(col) and bool(_ADDITIVE.search(col))


def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out, i = [0.0] * len(vals), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def corr_strength(r):
    a = abs(r)
    return ("praticamente nula" if a < 0.2 else "fraca" if a < 0.4 else
            "moderada" if a < 0.6 else "forte" if a < 0.8 else "muito forte")


def infer_types(cols, rows):
    types = {}
    for c in cols:
        vals = [r.get(c) for r in rows if r.get(c) is not None]
        n_num = sum(1 for v in vals if num(v) is not None)
        types[c] = "num" if vals and n_num / len(vals) >= 0.8 else "txt"
    return types


def build_facts(ev):
    """Fatos objetivos (em português) calculados sobre as linhas retornadas."""
    qid, cols, rows, types = ev["id"], ev["columns"], ev["rows"], ev["types"]
    n = len(rows)
    if n == 0:
        return [f"{qid}: a consulta não retornou linhas."]

    facts = []
    m = re.search(r"\blimit\s+(\d+)\s*(?:offset\s+\d+)?\s*;?\s*$", ev["sql"], re.I)
    if m and n >= int(m.group(1)):
        ev["truncated"] = True
        facts.append(f"{qid}: o resultado atingiu o LIMIT de {m.group(1)} linhas; podem existir mais linhas, "
                     f"então participações (%) valem apenas para as linhas retornadas.")

    label = next((c for c in cols if types[c] == "txt"), None)
    nums = [c for c in cols if types[c] == "num"]

    for c in nums[:4]:
        pairs = [(num(r.get(c)), short(r.get(label), 60) if label else f"linha {i + 1}")
                 for i, r in enumerate(rows) if num(r.get(c)) is not None]
        if len(pairs) < 2:
            continue
        vals = [p[0] for p in pairs]
        hi, lo = max(pairs, key=lambda p: p[0]), min(pairs, key=lambda p: p[0])
        line = f"{qid}: em '{c}', maior valor = {fmt(hi[0])} ({hi[1]}) e menor = {fmt(lo[0])} ({lo[1]})"
        if lo[0] > 0:
            line += f"; o maior é {fmt(hi[0] / lo[0])} vezes o menor"
        facts.append(line + ".")

        if len(vals) >= 5:
            mean, med, sd = statistics.fmean(vals), statistics.median(vals), statistics.stdev(vals)
            line = f"{qid}: em '{c}', média = {fmt(mean)} e mediana = {fmt(med)}"
            if mean > 0:
                line += f"; coeficiente de variação = {fmt(sd / mean * 100, 1)}%"
            if med and abs(mean - med) / abs(med) > 0.25:
                line += " (média e mediana divergem: distribuição assimétrica ou com outliers)"
            facts.append(line + ".")

        if len(vals) >= 8:
            q1, _, q3 = statistics.quantiles(vals, n=4, method="inclusive")
            iqr = q3 - q1
            if iqr > 0:
                out = [p for p in pairs if p[0] > q3 + 1.5 * iqr or p[0] < q1 - 1.5 * iqr]
                if out:
                    facts.append(f"{qid}: em '{c}', {len(out)} de {len(vals)} linhas são outliers pelo critério do IQR "
                                 f"(ex.: {out[0][1]} = {fmt(out[0][0])}).")

        if len(vals) >= 3 and min(vals) >= 0 and sum(vals) > 0 and is_additive(c):
            total, ordered = sum(vals), sorted(vals, reverse=True)
            acc, k80 = 0.0, len(ordered)
            for i, v in enumerate(ordered, 1):
                acc += v
                if acc / total >= 0.8:
                    k80 = i
                    break
            facts.append(f"{qid}: em '{c}', a maior linha ({hi[1]}) representa {fmt(ordered[0] / total * 100, 1)}% e as "
                         f"3 maiores {fmt(sum(ordered[:3]) / total * 100, 1)}% do total das {len(vals)} linhas retornadas; "
                         f"{k80} linhas somam 80% do total retornado.")

    for a, b in itertools.combinations(nums[:4], 2):
        xy = [(num(r.get(a)), num(r.get(b))) for r in rows]
        xy = [(x, y) for x, y in xy if x is not None and y is not None]
        if len(xy) < 8:
            continue
        xs, ys = zip(*xy)
        r = pearson(xs, ys)
        if r is None:
            continue
        rho = pearson(ranks(list(xs)), ranks(list(ys)))
        facts.append(f"{qid}: correlação entre '{a}' e '{b}': Pearson = {fmt(r)} ({corr_strength(r)})"
                     + (f", Spearman = {fmt(rho)}" if rho is not None else "")
                     + f" (n = {len(xy)} linhas retornadas; associação, não causalidade).")
    return facts


def register_evidence(state, objetivo, sql, result):
    cols = list(result.get("columns") or [])
    rows = rows_as_dicts(result)
    if not cols and rows:
        cols = list(rows[0].keys())
    ev = {"id": f"C{len(state['evidences']) + 1}", "objetivo": objetivo, "sql": sql,
          "columns": cols, "rows": rows, "types": infer_types(cols, rows), "truncated": False}
    ev["facts"] = build_facts(ev)
    state["evidences"].append(ev)
    state["query_succeeded"] = True
    return ev


def cell_text(v):
    return fmt(v) if num(v) is not None else ("—" if v is None else short(v, 50))


def render_rows(ev, max_rows):
    lines = [" | ".join(ev["columns"])]
    for r in ev["rows"][:max_rows]:
        lines.append(" | ".join(cell_text(r.get(c)) for c in ev["columns"]))
    if len(ev["rows"]) > max_rows:
        lines.append(f"... (+{len(ev['rows']) - max_rows} linhas)")
    return "\n".join(lines)


def evidence_digest(evidences, max_chars=DIGEST_CHARS):
    """Resumo compacto das evidências; reduz as amostras até caber no orçamento de contexto."""
    text = ""
    for sample in (12, 8, 5, 3):
        blocks = []
        for ev in evidences:
            sql = short(ev["sql"], 500)
            facts = "\n".join(f"- {f}" for f in ev["facts"][:10]) or "- (sem fatos adicionais)"
            blocks.append(f"[{ev['id']}] {ev['objetivo']}\nSQL: {sql}\nLinhas: {len(ev['rows'])}\n"
                          f"{render_rows(ev, sample)}\nFatos calculados:\n{facts}")
        text = "\n\n".join(blocks)
        if len(text) <= max_chars:
            return text
    return text[:max_chars]


# --------------------------------------------------------------------------
# Aprofundamento: o modelo critica a evidência e pede consultas complementares
# --------------------------------------------------------------------------
def deepen(question, plan, state, system, history_msgs):
    for round_no in range(1, MAX_DEEPENING_ROUNDS + 1):
        if state["sql_attempts"] >= MAX_SQL_ATTEMPTS - 1:
            break
        prompt = (
            f"PERGUNTA: {question}\n\nHIPÓTESES DO PLANO:\n" + "\n".join(f"- {h}" for h in plan["hipoteses"])
            + f"\n\nEVIDÊNCIAS ATÉ AGORA:\n{evidence_digest(state['evidences'], DIGEST_CHARS * 2 // 3)}\n\n"
            "Avalie criticamente como um revisor de análises:\n"
            "1. A evidência responde à pergunta? Falta alguma dimensão, comparação ou validação?\n"
            "2. Há resultados suspeitos (outliers, grupos pequenos, zeros que podem ser dado ausente, LIMIT truncando)?\n"
            "3. Alguma hipótese ficou sem teste?\n"
            f"Proponha no máximo {MAX_FOLLOWUPS_PER_ROUND} consultas complementares (DuckDB, SELECT com LIMIT, "
            "somente a tabela vendas) que reduzam essas dúvidas. Não repita consultas já executadas. "
            "Se a evidência já for suficiente, devolva a lista de consultas vazia."
        )
        data = chat_json([system] + history_msgs + [{"role": "user", "content": prompt}], REFLECT_SCHEMA,
                         temperature=0.2, required=("consultas",), retries=0, label=f"reflect_{round_no}")
        items = [q for q in (data or {}).get("consultas", [])
                 if isinstance(q, dict) and isinstance(q.get("sql"), str) and q["sql"].strip()]
        items = items[:MAX_FOLLOWUPS_PER_ROUND]
        if not items:
            emit(state, "Revisão crítica: evidência considerada suficiente.")
            break
        emit(state, f"Revisão crítica (rodada {round_no}): {len(items)} consulta(s) complementar(es).")
        before = len(state["evidences"])
        for item in items:
            run_with_repair(item, state, system)
        if len(state["evidences"]) == before:
            break


# --------------------------------------------------------------------------
# Verificação dos números citados (anti-alucinação)
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_ID_RE = re.compile(r"\bC\d+\b")


def number_candidates(tok):
    """Interpretações possíveis de um token numérico (pt-BR e en-US)."""
    if tok.isdigit():
        return {float(tok)}
    out = set()
    for s in (tok.replace(".", "").replace(",", "."), tok.replace(",", "")):
        try:
            out.add(float(s))
        except ValueError:
            pass
    return out


def numbers_in(text):
    out = set()
    for tok in _NUM_RE.findall(_ID_RE.sub(" ", str(text))):
        out |= number_candidates(tok)
    return out


class KnownNumbers:
    """Conjunto de números presentes nas evidências; aceita arredondamentos e 0,2% de tolerância."""

    def __init__(self, values):
        self.values = sorted(values)
        self.rounded = [{round(v, d) for v in self.values} for d in range(5)]

    def has(self, x):
        if any(x in s for s in self.rounded):
            return True
        i = bisect_left(self.values, x * 0.998)
        return i < len(self.values) and self.values[i] <= x * 1.002


def collect_known_numbers(state, history_msgs):
    known = {float(len(state["evidences"]))}
    for ev in state["evidences"]:
        known.add(float(len(ev["rows"])))
        known |= numbers_in(ev["sql"])
        for f in ev["facts"]:
            known |= numbers_in(f)
        for r in ev["rows"]:
            for v in r.values():
                f = num(v)
                if f is not None:
                    known.add(f)
                elif isinstance(v, str):
                    known |= numbers_in(v)
    known |= numbers_in(state["profile"].get("text", ""))
    for m in history_msgs:
        known |= numbers_in(m["content"])
    return KnownNumbers(known)


def unverified_numbers(text, known):
    bad = []
    for tok in _NUM_RE.findall(_ID_RE.sub(" ", text or "")):
        if tok.isdigit() and (int(tok) <= 10 or 1900 <= int(tok) <= 2100):
            continue
        cands = number_candidates(tok)
        if cands and not any(known.has(c) for c in cands) and tok not in bad:
            bad.append(tok)
    return bad


def find_unverified(struct, known):
    found = []
    texts = [("resumo", struct["resumo"])]
    for i, ins in enumerate(struct["insights"], 1):
        texts += [(f"insight {i}", " ".join([ins["titulo"], ins["evidencia"], ins["implicacao"]]))]
    texts += [(f"recomendação {i}", r) for i, r in enumerate(struct["recomendacoes"], 1)]
    for where, text in texts:
        for tok in unverified_numbers(text, known):
            found.append((where, tok))
    return found


# --------------------------------------------------------------------------
# Síntese
# --------------------------------------------------------------------------
_CONF = {"alta": "alta", "media": "média", "média": "média", "baixa": "baixa"}


def _s(v):
    return str(v).strip() if v is not None else ""


def _list(v):
    return v if isinstance(v, list) else []


def normalize_struct(raw, valid_ids, question):
    insights = []
    for i in _list(raw.get("insights")):
        if not isinstance(i, dict):
            continue
        item = {
            "titulo": _s(i.get("titulo")), "evidencia": _s(i.get("evidencia")),
            "implicacao": _s(i.get("implicacao")),
            "confianca": _CONF.get(_s(i.get("confianca")).lower(), "média"),
            "consultas": [c for c in (_s(x).upper() for x in _list(i.get("consultas"))) if c in valid_ids],
        }
        if item["titulo"] or item["evidencia"]:
            insights.append(item)
    return {
        "titulo": _s(raw.get("titulo")) or question.rstrip("?"),
        "resumo": _s(raw.get("resumo")),
        "kpis": [k for k in _list(raw.get("kpis")) if isinstance(k, dict)],
        "graficos": [g for g in _list(raw.get("graficos")) if isinstance(g, dict)],
        "tabelas": [t for t in _list(raw.get("tabelas")) if isinstance(t, dict)],
        "insights": insights,
        "recomendacoes": [t for t in (_s(x) for x in _list(raw.get("recomendacoes"))) if t],
        "limitacoes": [t for t in (_s(x) for x in _list(raw.get("limitacoes"))) if t],
        "proximos_passos": [t for t in (_s(x) for x in _list(raw.get("proximos_passos"))) if t],
    }


def synthesize(question, plan, state, system, history_msgs, known):
    valid_ids = {e["id"] for e in state["evidences"]}
    prompt = (
        f"PERGUNTA: {question}\n\nINTERPRETAÇÃO: {plan['interpretacao']}\n"
        "HIPÓTESES LEVANTADAS:\n" + "\n".join(f"- {h}" for h in plan["hipoteses"])
        + "\n\nEVIDÊNCIAS (única fonte de números; cada bloco tem um id [C1], [C2]...):\n"
        + evidence_digest(state["evidences"]) + "\n\n" + REPORT_INSTRUCTIONS
    )
    messages = [system] + history_msgs + [{"role": "user", "content": prompt}]
    required = tuple(REPORT_SCHEMA["required"])
    raw = chat_json(messages, REPORT_SCHEMA, temperature=0.2, required=required, retries=1, label="synthesis")
    if not raw:
        return None, []
    struct = normalize_struct(raw, valid_ids, question)
    bad = find_unverified(struct, known)

    if bad:
        emit(state, f"Conferindo {len(bad)} número(s) citado(s) que não aparecem nas evidências...")
        listing = "; ".join(f"{tok} ({where})" for where, tok in bad[:12])
        feedback = (
            "Os números a seguir aparecem nos textos, mas não constam nas evidências: " + listing + ". "
            "Reescreva o JSON completo corrigindo esses trechos com valores presentes nas evidências "
            "ou removendo a afirmação. Mantenha o restante."
        )
        revised_raw = chat_json(
            messages + [{"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                        {"role": "user", "content": feedback}],
            REPORT_SCHEMA, temperature=0.1, required=required, retries=0, label="synthesis_revision")
        if revised_raw:
            revised = normalize_struct(revised_raw, valid_ids, question)
            bad2 = find_unverified(revised, known)
            if len(bad2) <= len(bad) and revised["resumo"] and revised["insights"]:
                struct, bad = revised, bad2
    return struct, bad


# --------------------------------------------------------------------------
# Montagem do relatório (gráficos, tabelas e KPIs vêm dos dados, nunca do modelo)
# --------------------------------------------------------------------------
def build_chart(ev, x_col, y_col, title, tipo="bar", max_points=25):
    if x_col not in ev["columns"] or y_col not in ev["columns"]:
        return None
    pts = [(short(r.get(x_col), 40) if r.get(x_col) is not None else "—", num(r.get(y_col))) for r in ev["rows"]]
    pts = [(x, y) for x, y in pts if y is not None][:max_points]
    if len(pts) < 2:
        return None
    return {"tipo": tipo if tipo in SUPPORTED_CHART_TYPES else "bar", "titulo": title or f"{y_col} por {x_col}",
            "x": [p[0] for p in pts], "y": [round(p[1], 2) for p in pts]}


def auto_charts(evidences, limit=3):
    charts = []
    for ev in evidences:
        label = next((c for c in ev["columns"] if ev["types"][c] == "txt"), None)
        metric = next((c for c in ev["columns"] if ev["types"][c] == "num"), None)
        if label and metric:
            chart = build_chart(ev, label, metric, f"{metric} por {label} ({ev['id']})")
            if chart:
                charts.append(chart)
        if len(charts) >= limit:
            break
    return charts


def build_table(ev, title, max_rows=30):
    return {"titulo": title or ev["objetivo"], "colunas": ev["columns"],
            "linhas": [[clean_cell(r.get(c)) for c in ev["columns"]] for r in ev["rows"][:max_rows]]}


def _sentence(text):
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def format_insight(i):
    parts = [_sentence(i["titulo"]) if i["titulo"] else ""]
    if i["evidencia"]:
        parts.append("Evidência: " + _sentence(i["evidencia"]))
    if i["implicacao"]:
        parts.append("Implicação: " + _sentence(i["implicacao"]))
    parts.append(f"Confiança: {i['confianca']}." + (f" Fonte: {', '.join(i['consultas'])}." if i["consultas"] else ""))
    return " ".join(p for p in parts if p)


def build_sql_text(evidences):
    return "\n\n".join(f"-- {ev['id']}: {ev['objetivo']}\n{ev['sql']}" for ev in evidences)


def ensure_limitation(limits, pattern, text):
    if not any(re.search(pattern, item, re.I) for item in limits):
        limits.append(text)


def assemble_report(struct, state, unverified):
    evs = {e["id"]: e for e in state["evidences"]}

    kpis = []
    total = state["profile"].get("total_rows")
    if total:
        kpis.append({"label": f"Registros em {TABLE_NAME}", "valor": int(total)})
    for k in struct["kpis"]:
        ev, col, idx = evs.get(_s(k.get("consulta")).upper()), k.get("coluna"), k.get("linha")
        if not ev or col not in ev["columns"] or not isinstance(idx, int) or not 0 <= idx < len(ev["rows"]):
            continue
        val = ev["rows"][idx].get(col)
        f = num(val)
        kpis.append({"label": short(k.get("label") or col, 60),
                     "valor": round_clean(f) if f is not None else short(val, 60)})
    kpis = kpis[:6]

    charts = []
    for g in struct["graficos"]:
        ev = evs.get(_s(g.get("consulta")).upper())
        chart = build_chart(ev, g.get("x_coluna"), g.get("y_coluna"), _s(g.get("titulo")), g.get("tipo", "bar")) if ev else None
        if chart:
            charts.append(chart)
    charts = charts[:4] or auto_charts(state["evidences"])

    tables = []
    for t in struct["tabelas"]:
        ev = evs.get(_s(t.get("consulta")).upper())
        if ev and ev["rows"]:
            tables.append(build_table(ev, _s(t.get("titulo"))))
    tables = tables[:2] or [build_table(e, e["objetivo"]) for e in state["evidences"] if e["rows"]][:2]

    limits = list(struct["limitacoes"])
    ensure_limitation(limits, r"temporal|tend[eê]ncia|s[eé]rie|hist[oó]ric",
                      "Os dados são um retrato do catálogo, sem série temporal: não é possível afirmar tendência, crescimento ou sazonalidade.")
    if any(e["truncated"] for e in state["evidences"]):
        ensure_limitation(limits, r"limit", "Algumas consultas atingiram o LIMIT; participações e estatísticas valem apenas para as linhas retornadas.")
    if any("correlação" in f for e in state["evidences"] for f in e["facts"]):
        ensure_limitation(limits, r"correla", "Correlações indicam associação nas linhas retornadas e não implicam causalidade.")
    if unverified:
        toks = ", ".join(sorted({tok for _, tok in unverified})[:6])
        limits.append(f"Atenção: os valores {toks} citados nos textos não foram localizados automaticamente nas consultas; confirme-os antes de usar.")

    insights = [format_insight(i) for i in struct["insights"]]
    insights += [f"Recomendação: {_sentence(r)}" for r in struct["recomendacoes"]]
    insights += [f"Próximo passo: {_sentence(p)}" for p in struct["proximos_passos"]]

    final_struct = dict(struct, limitacoes=limits)
    report = {
        "titulo": struct["titulo"], "resumo": struct["resumo"], "kpis": kpis, "graficos": charts,
        "tabelas": tables, "insights": insights, "limitacoes": limits,
        "sql": build_sql_text(state["evidences"]),
    }
    return report, final_struct


def build_fallback_struct(question, state):
    """Relatório automático (sem interpretação do modelo), baseado nos fatos calculados pelo Python."""
    insights = []
    for ev in state["evidences"]:
        facts = ev["facts"][:3]
        if facts:
            insights.append({"titulo": f"Resultado de {ev['id']}: {ev['objetivo']}", "evidencia": " ".join(facts),
                             "implicacao": "", "confianca": "média", "consultas": [ev["id"]]})
    return {
        "titulo": question.rstrip("?"),
        "resumo": (f"Relatório automático baseado em {len(state['evidences'])} consulta(s) executada(s) no DuckDB. "
                   "A interpretação detalhada pelo modelo não foi gerada; os fatos abaixo foram calculados "
                   "diretamente pelo Python a partir dos resultados."),
        "kpis": [], "graficos": [], "tabelas": [], "insights": insights,
        "recomendacoes": [], "proximos_passos": [],
        "limitacoes": ["Relatório automático: apresenta resultados e estatísticas, mas não substitui uma análise interpretativa."],
    }


def compose_answer(struct, report_path):
    """Resposta de chat que complementa o dashboard com a narrativa da análise."""
    lines = [struct["resumo"]]
    if struct["insights"]:
        lines.append("\nPRINCIPAIS ACHADOS")
        for n, i in enumerate(struct["insights"], 1):
            text = f"{n}. {_sentence(i['titulo'])}" if i["titulo"] else f"{n}."
            if i["evidencia"]:
                text += f" {_sentence(i['evidencia'])}"
            if i["implicacao"]:
                text += f" Implicação: {_sentence(i['implicacao'])}"
            text += f" (confiança {i['confianca']}" + (f"; {', '.join(i['consultas'])}" if i["consultas"] else "") + ")"
            lines.append(text)
    if struct["recomendacoes"]:
        lines.append("\nRECOMENDAÇÕES")
        lines += [f"- {_sentence(r)}" for r in struct["recomendacoes"]]
    if struct["limitacoes"]:
        lines.append("\nCOMO INTERPRETAR (LIMITAÇÕES)")
        lines += [f"- {_sentence(x)}" for x in struct["limitacoes"]]
    if struct["proximos_passos"]:
        lines.append("\nPRÓXIMOS PASSOS")
        lines += [f"- {_sentence(x)}" for x in struct["proximos_passos"]]
    lines.append(f"\nO dashboard completo (gráficos, tabelas e SQL de cada consulta) está em: {report_path}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------
def create_agent_state(schema, on_progress=None):
    return {"schema": schema, "sql_attempts": 0, "query_succeeded": False, "report_generated": False,
            "report_path": None, "evidences": [], "seen_sql": set(), "profile": {},
            "on_progress": on_progress}


def clean_history(historico):
    msgs = []
    for m in (historico or [])[-HISTORY_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            msgs.append({"role": role, "content": content.strip()[:HISTORY_CHARS]})
    return msgs


def make_plan(question, system, history_msgs):
    prompt = f"PERGUNTA DO USUÁRIO:\n{question}\n\n{PLAN_INSTRUCTIONS}"
    data = chat_json([system] + history_msgs + [{"role": "user", "content": prompt}], PLAN_SCHEMA,
                     temperature=0.1, required=("consultas",), retries=1, label="plan")
    if not data:
        return None
    items = [{"objetivo": _s(q.get("objetivo"))[:140], "sql": q["sql"]}
             for q in _list(data.get("consultas"))
             if isinstance(q, dict) and isinstance(q.get("sql"), str) and q["sql"].strip()]
    if not items:
        return None
    return {"interpretacao": _s(data.get("interpretacao")), "tipo_analise": _s(data.get("tipo_analise")),
            "hipoteses": [t for t in (_s(h) for h in _list(data.get("hipoteses"))) if t][:5],
            "consultas": items[:MAX_PLAN_QUERIES]}


def run_agent(question, historico=None, on_progress=None):
    if not isinstance(question, str) or not question.strip():
        return {"success": False, "error": "A pergunta não pode estar vazia."}
    question = question.strip()

    try:
        schema = get_schema()
    except Exception as exc:
        return {"success": False, "error": f"Não foi possível carregar o esquema: {exc}"}
    if isinstance(schema, dict) and schema.get("success") is False:
        return {"success": False, "error": schema.get("error", "Falha ao carregar esquema.")}

    state = create_agent_state(schema, on_progress)
    history_msgs = clean_history(historico)
    write_log("user_question", {"question": question})

    try:
        emit(state, "Perfilando os dados...")
        try:
            state["profile"] = build_profile()
        except Exception as exc:
            write_log("profile_error", {"error": str(exc)})
            state["profile"] = {"text": "", "total_rows": None}

        system = {"role": "system", "content": PERSONA
                  + "\n\nESQUEMA REAL:\n" + json.dumps(schema, ensure_ascii=False, default=str)
                  + ("\n\nPERFIL DOS DADOS (calculado pelo Python; use como fonte confiável):\n"
                     + state["profile"]["text"] if state["profile"].get("text") else "")}

        emit(state, "Planejando a investigação...")
        plan = make_plan(question, system, history_msgs)
        if not plan:
            return {"success": False, "error": "O modelo não conseguiu planejar a análise. "
                    "Reformule a pergunta ou use um modelo maior (variável OLLAMA_MODEL)."}
        write_log("plan", plan)

        emit(state, f"Executando {len(plan['consultas'])} consulta(s) planejada(s)...")
        for item in plan["consultas"]:
            run_with_repair(item, state, system)
        if not state["evidences"]:
            return {"success": False, "error": "Nenhuma consulta foi concluída com sucesso."}

        deepen(question, plan, state, system, history_msgs)

        emit(state, "Escrevendo a análise...")
        known = collect_known_numbers(state, history_msgs)
        struct, unverified = None, []
        try:
            struct, unverified = synthesize(question, plan, state, system, history_msgs, known)
        except Exception as exc:
            write_log("synthesis_error", {"error": str(exc)})
        used_fallback = struct is None or not struct["resumo"] or not struct["insights"]
        if used_fallback:
            emit(state, "Síntese do modelo indisponível; gerando relatório automático.")
            struct, unverified = build_fallback_struct(question, state), []

        report_data, final_struct = assemble_report(struct, state, unverified)
        result = generate_report(report_data)
        if not result.get("success") and not used_fallback:
            write_log("report_error", {"error": result.get("error")})
            struct = build_fallback_struct(question, state)
            report_data, final_struct = assemble_report(struct, state, [])
            result = generate_report(report_data)
        if not result.get("success"):
            return {"success": False, "error": f"Falha ao gerar relatório: {result.get('error')}"}

        state["report_generated"] = True
        state["report_path"] = result.get("path")
    except Exception as exc:
        write_log("agent_error", {"error": str(exc)})
        return {"success": False, "error": str(exc)}

    answer = compose_answer(final_struct, state["report_path"])
    write_log("agent_finished", {"success": True, "report_path": state["report_path"],
                                 "sql_attempts": state["sql_attempts"], "queries_ok": len(state["evidences"])})
    return {"success": True, "answer": answer, "report_path": state["report_path"],
            "sql_attempts": state["sql_attempts"], "queries_ok": len(state["evidences"])}


if __name__ == "__main__":
    print("=" * 52)
    print("              IA ANALISTA DE DADOS")
    print("=" * 52)
    print("Modelo:", MODELO)
    historico = []
    while True:
        question = input("\nFaça sua pergunta (Enter vazio para sair): ").strip()
        if not question:
            break
        result = run_agent(question, historico)
        print("\nRESULTADO")
        if result["success"]:
            print(result["answer"])
            historico += [{"role": "user", "content": question}, {"role": "assistant", "content": result["answer"]}]
        else:
            print("Erro:", result["error"])
