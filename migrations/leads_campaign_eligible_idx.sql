-- Composite partial index for the dominant fetch_kjle_leads query shape.
-- WHERE is_active=true AND email_valid=true AND email IS NOT NULL AND pain_score >= N
-- ORDER BY pain_score DESC
CREATE INDEX CONCURRENTLY IF NOT EXISTS leads_campaign_eligible_idx
  ON public.leads (pain_score DESC, niche_slug)
  WHERE is_active = true
    AND email_valid = true
    AND email IS NOT NULL;

NOTIFY pgrst, 'reload schema';
