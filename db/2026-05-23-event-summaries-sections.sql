-- =============================================================================
-- event_summaries v2 — add `sections` JSONB column for dynamic Quartr-style
-- section headings (instead of the fixed 4 buckets in the v1 schema).
-- =============================================================================
-- New shape:
--   sections = [
--     { "heading": "Industry trends and demand drivers", "bullets": ["..."] },
--     { "heading": "Technology innovation and roadmap",  "bullets": ["..."] },
--     ...
--   ]
-- The old key_updates / operations / outlook / risks columns are kept so
-- older cached rows still render. New rows populate `sections`.
-- =============================================================================

ALTER TABLE event_summaries
    ADD COLUMN IF NOT EXISTS sections jsonb;

CREATE INDEX IF NOT EXISTS idx_event_summaries_has_sections
    ON event_summaries ((sections IS NOT NULL));

COMMENT ON COLUMN event_summaries.sections IS
    'Dynamic Quartr-style sections: [{heading, bullets[]}, ...]. Newer schema (May 23 2026).';
