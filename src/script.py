import duckdb

input_file = "data/raw/vendas.csv"
output_file = "data/curated/vendas.parquet"

duckdb.sql(f"""
    COPY (
        SELECT *
        FROM read_csv_auto('{input_file}')
    )
    TO '{output_file}'
    (FORMAT PARQUET);
""")

print("Conversão concluída!")
