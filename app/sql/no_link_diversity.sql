SELECT COUNT(DISTINCT p.domain_id) as num_domains, u.ap_id, u.user_name
FROM "user" u
INNER JOIN "post" p ON p.user_id = u.id
WHERE p.domain_id IS NOT null and u.post_count > 200 and u.post_reply_count < 20 and
p.posted_at >= NOW() - INTERVAL '30 days' and u.last_seen >= NOW() - INTERVAL '10 days'  and p."type" = 1 and
not (p.from_bot = true or p.from_reposter = true)
GROUP BY u.id
HAVING COUNT(DISTINCT p.domain_id) BETWEEN 1 AND 2