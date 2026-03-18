-- Migration: Add 'abandoned' to reminder_type enum
-- For automated reminders when consultations are abandoned
--
-- Run with: psql $DATABASE_URL -f migrations/add_abandoned_reminder_type.sql

ALTER TYPE reminder_type_enum ADD VALUE IF NOT EXISTS 'abandoned';
