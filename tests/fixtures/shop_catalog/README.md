# Catalog parser evidence

These gzip files contain exact EUC-KR listing-region bytes from public, unauthenticated
Cardmoa and TCGSHOP responses observed on 2026-10-11. Shared navigation before the
catalog count/search form was removed; listing products and pagination were retained.
No response headers, cookies, account data, or private datasets are included.

`manifest.json` records the source URL, observation time, original response SHA-256,
extracted fixture SHA-256, and independently checked page/product counts. Assertions
cover first/last pages, Cardmoa pages 2/235 (first/last links inside a page block), separate condition categories, discontinued products without
prices, legacy identifiers, and a Japanese card in a Korean category.

The fixtures represent those observations only; they do not represent current prices.
Full collection output and original responses belong in the database and ignored
`data/` directories, not Git.
