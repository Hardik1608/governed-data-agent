# Masking audit: analyst replica vs real customer ids

Warehouse `sales_real_replay.duckdb`; replica `masked.duckdb`; 5,942 real customer ids.

| Model | Column | Non-null values | Raw customer ids | Values not tokenised |
|---|---|---|---|---|
| dim_customer | customer_id | 8,906 | 0 | 0 |
| dim_customer | customer_sk | 8,906 | 0 | 0 |
| fct_order_lines | customer_id | 797,885 | 0 | 0 |
| fct_order_lines | customer_sk | 1,033,036 | 0 | 0 |
| fct_order_lines | order_line_id | 1,033,036 | 0 | 0 |
| fct_orders | customer_id | 44,876 | 0 | 0 |
| fct_orders | customer_sk | 53,628 | 0 | 0 |

Re-identification attack on the dbt key `customer_sk = md5(customer_id || '|' || ts)`, trying all 5,942 ids with each row's `valid_from` and `first_invoice_at`:

| Target | Keys attacked | Keys re-identified |
|---|---|---|
| source warehouse (dbt keys, positive control) | 8,905 | 8,725 |
| analyst replica (re-keyed) | 8,905 | 0 |
