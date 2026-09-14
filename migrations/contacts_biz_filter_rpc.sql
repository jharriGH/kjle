-- KJLE — contacts_biz_count + contacts_biz_rows RPCs
-- Purpose: JOIN contacts → leads for linked_no_chatbot / linked_low_a11y filters.
-- contacts.has_chatbot is NULL (not denormalized), so the join is required.
-- Indexes used: idx_contacts_business_id (contacts), idx_leads_has_chatbot (leads).
-- Applied: 2026-09-14

CREATE OR REPLACE FUNCTION contacts_biz_count(
  p_niche_slug          text      DEFAULT NULL,
  p_seniority           text[]    DEFAULT NULL,
  p_title               text      DEFAULT NULL,
  p_company             text      DEFAULT NULL,
  p_has_company_website boolean   DEFAULT NULL,
  p_email_provider      text[]    DEFAULT NULL,
  p_email_trust         text[]    DEFAULT NULL,
  p_email_status        text      DEFAULT NULL,
  p_state               text      DEFAULT NULL,
  p_min_word_count      integer   DEFAULT NULL,
  p_linked_no_chatbot   boolean   DEFAULT NULL,
  p_linked_low_a11y     integer   DEFAULT NULL
) RETURNS bigint
LANGUAGE sql STABLE PARALLEL SAFE SECURITY DEFINER AS $$
  SELECT COUNT(*)::bigint
  FROM contacts c
  JOIN leads l ON c.business_id = l.id
  WHERE
    (p_niche_slug IS NULL OR c.niche_slug = p_niche_slug)
    AND (p_seniority IS NULL OR c.seniority = ANY(p_seniority))
    AND (p_title IS NULL OR c.title ILIKE '%' || p_title || '%')
    AND (p_company IS NULL OR c.company ILIKE '%' || p_company || '%')
    AND (
      p_has_company_website IS NULL
      OR (p_has_company_website = true AND c.company_website IS NOT NULL)
      OR (p_has_company_website = false AND c.company_website IS NULL)
    )
    AND (p_email_provider IS NULL OR c.email_provider = ANY(p_email_provider))
    AND (p_email_trust IS NULL OR c.email_trust = ANY(p_email_trust))
    AND (p_email_status IS NULL OR c.email_status = p_email_status)
    AND (p_state IS NULL OR c.state = p_state)
    AND (p_min_word_count IS NULL OR c.website_word_count >= p_min_word_count)
    AND (
      p_linked_no_chatbot IS NULL
      OR (p_linked_no_chatbot = true  AND l.has_chatbot = false)
      OR (p_linked_no_chatbot = false AND l.has_chatbot = true)
    )
    AND (p_linked_low_a11y IS NULL OR l.accessibility_score < p_linked_low_a11y)
$$;

GRANT EXECUTE ON FUNCTION contacts_biz_count(
  text, text[], text, text, boolean, text[], text[], text, text, integer, boolean, integer
) TO anon, authenticated;


CREATE OR REPLACE FUNCTION contacts_biz_rows(
  p_niche_slug          text      DEFAULT NULL,
  p_seniority           text[]    DEFAULT NULL,
  p_title               text      DEFAULT NULL,
  p_company             text      DEFAULT NULL,
  p_has_company_website boolean   DEFAULT NULL,
  p_email_provider      text[]    DEFAULT NULL,
  p_email_trust         text[]    DEFAULT NULL,
  p_email_status        text      DEFAULT NULL,
  p_state               text      DEFAULT NULL,
  p_min_word_count      integer   DEFAULT NULL,
  p_linked_no_chatbot   boolean   DEFAULT NULL,
  p_linked_low_a11y     integer   DEFAULT NULL,
  p_limit               integer   DEFAULT 50,
  p_offset              integer   DEFAULT 0
) RETURNS TABLE (
  id                         uuid,
  full_name                  text,
  title                      text,
  seniority                  text,
  company                    text,
  company_website            text,
  primary_email              text,
  niche_slug                 text,
  city                       text,
  state                      text,
  email_provider             text,
  email_trust                text,
  email_status               text,
  has_chatbot                boolean,
  accessibility_score        integer,
  website_word_count         integer,
  linkedin_url               text,
  business_id                uuid,
  linked_business_name       text,
  linked_has_chatbot         boolean,
  linked_accessibility_score integer,
  linked_website             text,
  linked_pain_score          numeric
)
LANGUAGE sql STABLE PARALLEL SAFE SECURITY DEFINER AS $$
  SELECT
    c.id, c.full_name, c.title, c.seniority, c.company, c.company_website,
    c.primary_email, c.niche_slug, c.city, c.state, c.email_provider,
    c.email_trust, c.email_status, c.has_chatbot, c.accessibility_score,
    c.website_word_count, c.linkedin_url, c.business_id,
    l.business_name                   AS linked_business_name,
    l.has_chatbot                     AS linked_has_chatbot,
    l.accessibility_score             AS linked_accessibility_score,
    l.website                         AS linked_website,
    l.pain_score                      AS linked_pain_score
  FROM contacts c
  JOIN leads l ON c.business_id = l.id
  WHERE
    (p_niche_slug IS NULL OR c.niche_slug = p_niche_slug)
    AND (p_seniority IS NULL OR c.seniority = ANY(p_seniority))
    AND (p_title IS NULL OR c.title ILIKE '%' || p_title || '%')
    AND (p_company IS NULL OR c.company ILIKE '%' || p_company || '%')
    AND (
      p_has_company_website IS NULL
      OR (p_has_company_website = true AND c.company_website IS NOT NULL)
      OR (p_has_company_website = false AND c.company_website IS NULL)
    )
    AND (p_email_provider IS NULL OR c.email_provider = ANY(p_email_provider))
    AND (p_email_trust IS NULL OR c.email_trust = ANY(p_email_trust))
    AND (p_email_status IS NULL OR c.email_status = p_email_status)
    AND (p_state IS NULL OR c.state = p_state)
    AND (p_min_word_count IS NULL OR c.website_word_count >= p_min_word_count)
    AND (
      p_linked_no_chatbot IS NULL
      OR (p_linked_no_chatbot = true  AND l.has_chatbot = false)
      OR (p_linked_no_chatbot = false AND l.has_chatbot = true)
    )
    AND (p_linked_low_a11y IS NULL OR l.accessibility_score < p_linked_low_a11y)
  ORDER BY c.created_at DESC NULLS LAST
  LIMIT p_limit OFFSET p_offset
$$;

GRANT EXECUTE ON FUNCTION contacts_biz_rows(
  text, text[], text, text, boolean, text[], text[], text, text, integer, boolean, integer, integer, integer
) TO anon, authenticated;
