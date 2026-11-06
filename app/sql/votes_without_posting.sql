WITH votes AS (
    SELECT user_id, effect, created_at
    FROM post_vote
    WHERE effect <> 0
      AND created_at > now() - interval '2 months'
    UNION ALL
    SELECT user_id, effect, created_at
    FROM post_reply_vote
    WHERE effect <> 0
      AND created_at > now() - interval '2 months'
)
SELECT v.user_id,
       voter.user_name AS voter,
       voter.ap_domain AS voter_domain,
       voter.ap_id     AS voter_ap_id,
       count(*)                           AS votes,
       count(*) FILTER (WHERE effect > 0) AS upvotes,
       count(*) FILTER (WHERE effect < 0) AS downvotes,
       max(v.created_at)                  AS last_vote
FROM votes v
JOIN "user" voter ON voter.id = v.user_id
WHERE voter.post_count = 0
  AND voter.post_reply_count = 0
  AND voter.deleted = false
  AND voter.bot = false
  AND (voter.bot_override = false OR voter.bot_override IS NULL)
GROUP BY v.user_id, voter.user_name, voter.ap_domain, voter.ap_id
HAVING count(*) >= 20
ORDER BY votes DESC, last_vote DESC;
