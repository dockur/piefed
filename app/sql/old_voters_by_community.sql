WITH params AS (
    SELECT 100 AS min_votes   -- ignore communities with fewer total votes
),
votes AS (
    SELECT p.community_id, pv.user_id
    FROM post_vote pv
    JOIN post p ON p.id = pv.post_id
    WHERE pv.effect <> 0
    UNION ALL
    SELECT r.community_id, prv.user_id
    FROM post_reply_vote prv
    JOIN post_reply r ON r.id = prv.post_reply_id
    WHERE prv.effect <> 0
),
classified AS (
    SELECT v.community_id,
           CASE
               WHEN u.last_seen IS NULL                      THEN 'unknown (never seen)'
               WHEN u.last_seen < NOW() - INTERVAL '6 months' THEN 'blocked'
               ELSE 'accepted'
           END AS bucket
    FROM votes v
    LEFT JOIN "user" u ON u.id = v.user_id
    CROSS JOIN params p
)
SELECT c.id,
       c.ap_id,
       c.title,
       COUNT(*)                                        AS total_votes,
       COUNT(*) FILTER (WHERE bucket = 'blocked')     AS blocked_votes,
       ROUND(100.0 * COUNT(*) FILTER (WHERE bucket = 'blocked') / NULLIF(COUNT(*), 0), 2) AS pct_blocked,
       COUNT(*) FILTER (WHERE bucket = 'unknown (never seen)') AS null_last_seen_votes
FROM classified v
JOIN community c ON c.id = v.community_id
GROUP BY c.id, c.ap_id, c.title
HAVING COUNT(*) >= (SELECT min_votes FROM params)
ORDER BY pct_blocked DESC, total_votes DESC;
