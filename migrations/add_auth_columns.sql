-- Migration: Add multi-method authentication columns to users table
-- Run this ONCE against your production database before deploying the new code.
-- These are all safe ALTER TABLE ADD COLUMN IF NOT EXISTS operations.

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_hash VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_expires TIMESTAMP WITH TIME ZONE;

ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp_hash VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp_expires TIMESTAMP WITH TIME ZONE;

ALTER TABLE users ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS two_factor_secret_encrypted VARCHAR(500);

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500);

-- Add unique index on google_id for OAuth lookups
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id) WHERE google_id IS NOT NULL;

-- Mark all existing users as verified (they registered before email verification existed)
UPDATE users SET is_verified = true WHERE is_verified = false;
