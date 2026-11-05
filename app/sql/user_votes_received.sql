WITH v AS (
    SELECT pv.user_id AS other_id, pv.effect
    FROM post_vote pv
    WHERE pv.author_id = :user_id AND pv.effect <> 0 AND pv.user_id <> pv.author_id
      AND pv.created_at >= NOW() - make_interval(days => :days)
    UNION ALL
    SELECT prv.user_id, prv.effect
    FROM post_reply_vote prv
    WHERE prv.author_id = :user_id AND prv.effect <> 0 AND prv.user_id <> prv.author_id
      AND prv.created_at >= NOW() - make_interval(days => :days)
),
agg AS (
    SELECT other_id,
           COUNT(*) AS votes,
           COUNT(*) FILTER (WHERE effect > 0) AS ups,
           COUNT(*) FILTER (WHERE effect < 0) AS downs
    FROM v GROUP BY other_id
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY votes DESC, other_id) AS rn,
              SUM(votes) OVER () AS grand_total
    FROM agg
)
SELECT 0 AS sort_order, r.other_id AS user_id,
       COALESCE(u.ap_id, u.user_name) AS label,
       r.votes, r.ups, r.downs,
       ROUND(100.0 * r.votes / r.grand_total, 2) AS pct,
       u.post_count, u.post_reply_count
FROM ranked r JOIN "user" u ON u.id = r.other_id
WHERE r.rn <= :top_n
UNION ALL
SELECT 1, NULL,
       'Others (' || COUNT(*) || ' accounts)',
       CAST(SUM(votes) AS bigint), CAST(SUM(ups) AS bigint), CAST(SUM(downs) AS bigint),
       ROUND(100.0 * SUM(votes) / MAX(grand_total), 2), 1, 1
FROM ranked WHERE rn > :top_n
HAVING COUNT(*) > 0
ORDER BY sort_order, votes DESC;