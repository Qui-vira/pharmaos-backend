-- Migration: Add global unique constraint on patient phone number
-- One-phone-one-pharmacy rule: each phone can only be registered at one pharmacy
--
-- IMPORTANT: Before running, verify no duplicate phones exist across orgs:
--   SELECT phone, COUNT(*) FROM patients GROUP BY phone HAVING COUNT(*) > 1;
--
-- Run with: psql $DATABASE_URL -f migrations/add_patient_phone_global_unique.sql

-- Add global unique constraint (phone can only exist once across all pharmacies)
ALTER TABLE patients
    ADD CONSTRAINT uq_patient_phone_global UNIQUE (phone);
