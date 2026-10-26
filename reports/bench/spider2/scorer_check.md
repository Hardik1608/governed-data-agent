# Spider2-lite scorer check (published gold SQL vs published gold results)

Local tasks: 135; with published gold SQL: 24. Each gold query was run read-only on the task's SQLite file and scored with this project's port of `compare_pandas_table`.

| Outcome | Tasks |
|---|---|
| gold SQL failed | 2 |
| scored correct | 14 |
| scored wrong | 8 |

Gold queries rejected by the safety validator: 0.

| Task | Database | Outcome | Detail |
|---|---|---|---|
| local003 | E_commerce | scored wrong | pred_rows 11, gold_rows 9, pred_cols 2, gold_cols 2, gold_variants 4 |
| local004 | E_commerce | scored correct |  |
| local008 | Baseball | scored correct |  |
| local017 | California_Traffic_Collision | scored correct |  |
| local019 | WWE | scored correct |  |
| local022 | IPL | scored correct |  |
| local023 | IPL | scored wrong | pred_rows 5, gold_rows 5, pred_cols 2, gold_cols 3, gold_variants 1 |
| local029 | Brazilian_E_Commerce | scored wrong | pred_rows 3, gold_rows 3, pred_cols 3, gold_cols 5, gold_variants 2 |
| local038 | Pagila | scored correct |  |
| local039 | Pagila | scored correct |  |
| local058 | education_business | scored correct |  |
| local066 | modern_data | scored wrong | pred_rows 12, gold_rows 12, pred_cols 2, gold_cols 2, gold_variants 3 |
| local065 | modern_data | scored correct |  |
| local075 | bank_sales_trading | gold SQL failed | Query exceeded the 120s time limit |
| local078 | bank_sales_trading | scored correct |  |
| local099 | Db-IMDB | gold SQL failed | Query exceeded the 120s time limit |
| local131 | EntertainmentAgency | scored wrong | pred_rows 20, gold_rows 25, pred_cols 4, gold_cols 5, gold_variants 1 |
| local163 | education_business | scored correct |  |
| local197 | sqlite-sakila | scored correct |  |
| local199 | sqlite-sakila | scored correct |  |
| local210 | delivery_center | scored wrong | pred_rows 21, gold_rows 21, pred_cols 1, gold_cols 6, gold_variants 2 |
| local219 | EU_soccer | scored wrong | pred_rows 11, gold_rows 11, pred_cols 2, gold_cols 3, gold_variants 6 |
| local301 | bank_sales_trading | scored correct |  |
| local309 | f1 | scored wrong | pred_rows 75, gold_rows 75, pred_cols 3, gold_cols 3, gold_variants 3 |
