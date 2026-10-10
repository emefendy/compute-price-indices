# data/

Filled in by `gpu-prices run` (and the daily GitHub Action). Empty until the first real collection.

- `raw/<YYYY-MM-DD>/<provider>.json` – untouched API responses
- `clean/prices.csv` – normalized quotes across all snapshots
- `clean/rejections.csv` – raw records filtered out during cleaning, by reason
- `clean/issues.csv` – data-quality check results

Synthetic test data lives in `tests/fixtures/`, never here, so nothing in this folder is made up.
