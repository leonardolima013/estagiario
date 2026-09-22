import os

import matplotlib.pyplot as plt
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# Duplicatas — Caso 1: registros com search_ref e brand_id iguais.
conn = psycopg2.connect(
    host=os.environ["DB_HOST"],
    port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)

with conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT *
        FROM (
            SELECT
                cp.id, cp.search_ref, cp.brand_id, mb.name AS brand, cp.name,
                cp.width, cp.depth, cp.height, cp.gross_weight, cp.net_weight,
                cp.similarity_id, cp.born_at, cp.deprecated_at, cp.application,
                COUNT(*) OVER (PARTITION BY cp.search_ref, cp.brand_id) AS qtd_duplicados
            FROM catalog_part cp
            JOIN manufacturer_brand mb ON mb.id = cp.brand_id
            WHERE cp.search_ref IS NOT NULL AND cp.search_ref <> ''
        ) t
        WHERE qtd_duplicados > 1
        ORDER BY search_ref, brand_id, id
        """
    )
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]

conn.close()

dup_search_ref_brand_df = pd.DataFrame(rows, columns=columns)
print(f"{len(dup_search_ref_brand_df)} registros duplicados")
print(f"{dup_search_ref_brand_df.groupby(['search_ref', 'brand_id']).ngroups} grupos (search_ref, brand_id) duplicados")
dup_search_ref_brand_df

# Duplicatas — Caso 2: registros com brand_id e name iguais.
conn = psycopg2.connect(
    host=os.environ["DB_HOST"],
    port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)

with conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT *
        FROM (
            SELECT
                cp.id, cp.search_ref, cp.brand_id, mb.name AS brand, cp.name,
                cp.width, cp.depth, cp.height, cp.gross_weight, cp.net_weight,
                cp.similarity_id, cp.born_at, cp.deprecated_at, cp.application,
                COUNT(*) OVER (PARTITION BY cp.brand_id, cp.name) AS qtd_duplicados
            FROM catalog_part cp
            JOIN manufacturer_brand mb ON mb.id = cp.brand_id
            WHERE cp.name IS NOT NULL AND cp.name <> ''
        ) t
        WHERE qtd_duplicados > 1
        ORDER BY brand_id, name, id
        """
    )
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]

conn.close()

dup_brand_name_df = pd.DataFrame(rows, columns=columns)
print(f"{len(dup_brand_name_df)} registros duplicados")
print(f"{dup_brand_name_df.groupby(['brand_id', 'name']).ngroups} grupos (brand_id, name) duplicados")
dup_brand_name_df