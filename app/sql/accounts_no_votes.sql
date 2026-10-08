SELECT u.id, u.user_name, u.ap_id, u.ap_profile_id, u.bot, u.bot_override, u.reposter, u.reposter_override,
       p.post_count,
       COALESCE(v.other_votes, 0) AS votes_on_others,
       COALESCE(v.self_votes, 0)  AS self_votes
FROM (
    SELECT user_id, COUNT(*) AS post_count
    FROM post
    WHERE posted_at > NOW() - INTERVAL '7 days'
      AND deleted = false
    GROUP BY user_id
    HAVING COUNT(*) > 10
) p
JOIN "user" u ON u.id = p.user_id
JOIN instance i ON i.id = u.instance_id
LEFT JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE pv.author_id IS DISTINCT FROM pv.user_id) AS other_votes,
           COUNT(*) FILTER (WHERE pv.author_id = pv.user_id)                AS self_votes
    FROM post_vote pv
    WHERE pv.user_id = p.user_id
      AND pv.created_at > NOW() - INTERVAL '7 days'
) v ON true
WHERE COALESCE(v.other_votes, 0) = 0
  AND LOWER(i.software) IN ('lemmy', 'piefed')
  AND u.deleted = false
  AND u.banned = false
  and u.bot = false and (u.bot_override = false or u.bot_override is null)
ORDER BY p.post_count DESC, votes_on_others ASC;