-- #875 §13: how often do contradictions occur at all? Read-only.
-- Subject + functional predicate GROUP with more than one live edge.
-- Works before and after stage 1: with valid_to present, add "AND (r.valid_to IS NULL OR r.valid_to > now())"
-- to the WHERE clause; before stage 1 every active edge is live.
WITH grouped AS (
  SELECT r.subject_id,
         CASE
           WHEN r.predicate ~ '^(wohnt_in|lebt_in|lives_in|resides_in)$' THEN 'residence'
           WHEN r.predicate ~ '^(arbeitet_bei|works_at|works_for)$'     THEN 'employer'
         END AS grp,
         r.object_id
  FROM kg_relations r
  WHERE r.is_active = true
)
SELECT grp,
       count(*) FILTER (WHERE n > 1)       AS subjects_with_conflict,
       count(*)                            AS subjects_total,
       coalesce(max(n), 0)                 AS max_objects_per_subject
FROM (
  SELECT subject_id, grp, count(DISTINCT object_id) AS n
  FROM grouped WHERE grp IS NOT NULL
  GROUP BY subject_id, grp
) s
GROUP BY grp
ORDER BY grp;
