-- Telepharmacy Sessions Migration
-- Adds support for remote pharmacist consultations (video/voice/chat)

-- Session type enum
DO $$ BEGIN
    CREATE TYPE telepharmacy_session_type_enum AS ENUM ('video', 'voice', 'chat');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Session status enum
DO $$ BEGIN
    CREATE TYPE telepharmacy_status_enum AS ENUM ('waiting', 'ringing', 'active', 'completed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Telepharmacy sessions table
CREATE TABLE IF NOT EXISTS telepharmacy_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    patient_id UUID NOT NULL REFERENCES patients(id),
    pharmacist_id UUID REFERENCES users(id),
    session_type telepharmacy_session_type_enum NOT NULL DEFAULT 'video',
    status telepharmacy_status_enum NOT NULL DEFAULT 'waiting',
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    duration_seconds INTEGER,
    recording_url TEXT,
    notes TEXT,
    prescription JSONB,
    consultation_id UUID REFERENCES consultations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS ix_telepharmacy_org_status ON telepharmacy_sessions (org_id, status);
CREATE INDEX IF NOT EXISTS ix_telepharmacy_pharmacist ON telepharmacy_sessions (pharmacist_id);
