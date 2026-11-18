Invoice PDF extraction project

1. Install dependencies: pip install -r requirements.txt
2. Set PGHOST, PGPORT, PGDATABASE, PGUSER, and PGPASSWORD in the environment or in .env.
3. Run postgres_schema.sql in two steps: first create invoice_extraction_db while connected to the PostgreSQL maintenance database (usually postgres), then connect to invoice_extraction_db and create invoice_details.
4. Run: python invoice_extractor.py "C:/path/to/pdf-folder"

Duplicate protection: invoice_no is a required unique key. Re-importing an existing invoice number updates its extracted fields when new values are available. PDFs without a valid M0 invoice number are skipped because they cannot be safely deduplicated. Replace any placeholder password in .env before running.
