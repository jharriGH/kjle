-- Bulk contactability update function for Layer 5B backlog scrub.
-- Accepts a JSONB array of classification records and updates leads in one statement.
-- Returns count of rows updated.
CREATE OR REPLACE FUNCTION public.bulk_update_lead_contactability(
  p_updates JSONB
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_count INTEGER;
BEGIN
  WITH updates AS (
    SELECT
      (elem->>'id')::uuid AS id,
      (elem->>'contactable')::boolean AS contactable,
      COALESCE(elem->'reasons', '[]'::jsonb) AS filter_reasons,
      (elem->>'classified_at')::timestamptz AS filter_classified_at,
      (elem->>'label')::text AS filter_classified_by
    FROM jsonb_array_elements(p_updates) AS elem
  )
  UPDATE leads l
  SET
    contactable = u.contactable,
    filter_reasons = u.filter_reasons,
    filter_classified_at = u.filter_classified_at,
    filter_classified_by = u.filter_classified_by
  FROM updates u
  WHERE l.id = u.id;

  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.bulk_update_lead_contactability(JSONB) TO service_role, authenticated, anon;

-- Schema reload notify for PostgREST
NOTIFY pgrst, 'reload schema';
