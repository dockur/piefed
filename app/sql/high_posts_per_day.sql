WITH user_post_stats AS (
    SELECT
        user_id,
        COUNT(*) as post_count,
        MIN(posted_at) as first_post_date,
        MAX(posted_at) as last_post_date
    FROM post
    WHERE deleted = false
    GROUP BY user_id
    HAVING COUNT(*) > 0
)
SELECT
    u.id as user_id,
    u.user_name, u.ap_id,
    ups.post_count,
    ups.first_post_date,
    ups.last_post_date,
    ROUND(
        ups.post_count::numeric /
        NULLIF(
            EXTRACT(EPOCH FROM (ups.last_post_date - ups.first_post_date)) / 86400 + 1,
            0
        ),
        1
    ) as avg_posts_per_day
FROM user_post_stats ups
JOIN "user" u ON ups.user_id = u.id
where u.deleted = false and u.banned = false and not (u.bot = true or u.bot_override = true)
ORDER BY avg_posts_per_day DESC
LIMIT 100;