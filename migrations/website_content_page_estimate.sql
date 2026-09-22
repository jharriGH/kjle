-- Migration: add website_content_page_estimate column
-- Companion to website_internal_page_count; excludes utility/nav/legal/asset paths
-- so BizReply gets a tighter estimate of real content pages.
-- Nullable INT; filled going forward by batch-free audit; no backfill.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_content_page_estimate INTEGER;
