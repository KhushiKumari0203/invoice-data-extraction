-- Run this once while connected to the PostgreSQL maintenance database (usually postgres).
CREATE DATABASE invoice_extraction_test_db;

-- Then connect to invoice_extraction_db and run the table definition below.
CREATE TABLE IF NOT EXISTS invoice_details (
    id SERIAL PRIMARY KEY,
    booking_id VARCHAR(50) CHECK (booking_id IS NULL OR booking_id ~ '^NH'),
    invoice_no VARCHAR(50) NOT NULL UNIQUE CHECK (invoice_no ~ '^M0'),
    invoice_date DATE,
    hsn_sac TEXT,
    mmt_gstin VARCHAR(20),
    mmt_pan VARCHAR(15),
    company_address TEXT,
    company_gstin VARCHAR(20),
    customer_or_passanger_name VARCHAR(255),
    basic_amount NUMERIC(12,2),
    grand_total NUMERIC(12,2),
    hotel_name VARCHAR(255),
    hotel_city VARCHAR(100),
    check_in_date DATE,
    check_out_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (check_out_date IS NULL OR check_in_date IS NULL OR check_out_date >= check_in_date)
);

-- Upgrade an existing table created with the older, length-limited column.
ALTER TABLE invoice_details ALTER COLUMN hsn_sac TYPE TEXT;
