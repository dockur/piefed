WITH v AS MATERIALIZED (
    SELECT user_id, author_id, effect, created_at, post_id, NULL::integer AS post_reply_id
    FROM post_vote
    WHERE created_at >= NOW() - make_interval(days => :days)
      AND effect <> 0 AND author_id IS NOT NULL AND user_id <> author_id
    UNION ALL
    SELECT user_id, author_id, effect, created_at, NULL::integer, post_reply_id
    FROM post_reply_vote
    WHERE created_at >= NOW() - make_interval(days => :days)
      AND effect <> 0 AND author_id IS NOT NULL AND user_id <> author_id
),
all_pairs AS (
    SELECT user_id, author_id,
           COUNT(*) AS votes,
           COUNT(*) FILTER (WHERE effect > 0) AS ups,
           COUNT(*) FILTER (WHERE effect < 0) AS downs,
           MIN(created_at) AS first_vote,
           MAX(created_at) AS last_vote,
           SUM(COUNT(*)) OVER (PARTITION BY user_id) AS total
    FROM v
    GROUP BY user_id, author_id
),
candidates AS (
    SELECT p.*
    FROM all_pairs p
    JOIN "user" voter  ON voter.id  = p.user_id
    JOIN "user" target ON target.id = p.author_id
    WHERE p.votes >= :min_votes
      AND (p.ups = 0 OR p.downs = 0)
      AND p.votes::float / p.total >= :min_focus
      AND voter.deleted IS NOT TRUE AND voter.banned IS NOT TRUE
      AND target.deleted IS NOT TRUE AND target.bot IS NOT TRUE AND target.bot_override IS NOT TRUE AND target.banned IS NOT TRUE
),
detail AS (
    SELECT c.user_id, c.author_id,
           COUNT(DISTINCT COALESCE(po.community_id, pr.community_id)) AS communities,
           COUNT(*)::float
               / GREATEST(COUNT(DISTINCT date_trunc('minute', v.created_at)), 1) AS per_min
    FROM candidates c
    JOIN v ON v.user_id = c.user_id AND v.author_id = c.author_id
    LEFT JOIN post po       ON po.id = v.post_id
    LEFT JOIN post_reply pr ON pr.id = v.post_reply_id
    GROUP BY c.user_id, c.author_id
)
SELECT COALESCE(voter.ap_id, voter.user_name)   AS voter_label,
       COALESCE(target.ap_id, target.user_name) AS target_label,
       c.votes, c.ups, c.downs, d.communities,
       ROUND(100.0 * c.votes / c.total)                  AS share_pct,
       EXTRACT(EPOCH FROM (c.last_vote - c.first_vote))  AS span_seconds,
       d.per_min
FROM candidates c
JOIN detail d      ON d.user_id = c.user_id AND d.author_id = c.author_id
JOIN "user" voter  ON voter.id  = c.user_id
JOIN "user" target ON target.id = c.author_id
WHERE (d.communities >= :min_communities OR d.per_min >= :min_rate)
ORDER BY c.votes DESC
LIMIT 50
