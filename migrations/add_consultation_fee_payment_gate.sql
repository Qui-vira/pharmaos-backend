-- Migration: Add consultation fee payment gate
-- New status 'awaiting_payment' between intake and pending_review
-- consultation_fee_paid tracks whether the fee has been collected
--
-- Run with: psql $DATABASE_URL -f migrations/add_consultation_fee_payment_gate.sql

-- Add awaiting_payment to the consultation status enum
ALTER TYPE consultation_status_enum ADD VALUE IF NOT EXISTS 'awaiting_payment' BEFORE 'pending_review';

-- Add consultation_fee_paid column
ALTER TABLE consultations
    ADD COLUMN IF NOT EXISTS consultation_fee_paid BOOLEAN NOT NULL DEFAULT FALSE;
