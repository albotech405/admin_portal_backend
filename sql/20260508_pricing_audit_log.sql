-- Create pricing_audit_log table
-- Run this in the Supabase SQL editor (Dashboard → SQL editor)

CREATE TABLE IF NOT EXISTS public.pricing_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id TEXT NOT NULL,
    change_type TEXT NOT NULL,          -- 'vehicle_pricing' | 'global_config' | 'category_multiplier'
    change_summary TEXT NOT NULL DEFAULT '',
    previous_values JSONB,
    new_values JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enable RLS (optional but recommended)
ALTER TABLE public.pricing_audit_log ENABLE ROW LEVEL SECURITY;

-- Allow service role full access
CREATE POLICY "service_role_all" ON public.pricing_audit_log
    FOR ALL TO service_role USING (true) WITH CHECK (true);
