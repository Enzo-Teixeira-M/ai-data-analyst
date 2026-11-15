
import duckdb

DB_PATH = "database/analytics.duckdb"

con = duckdb.connect(DB_PATH)

con.execute("""
    CREATE OR REPLACE VIEW vendas AS
    SELECT *
    FROM read_parquet('data/curated/vendas.parquet');
""")

print("Banco de dados criado!")
print("View 'vendas' criada com sucesso.")

resultado = con.execute("""
    SELECT *
    FROM vendas
    LIMIT 10;
""").df()

print(resultado)
con.close()