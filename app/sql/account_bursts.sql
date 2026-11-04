-- Second way into voting_clusters for throwaway farms that the main pair query cannot see.
--
-- Disposable accounts cast ~20 votes each, mostly on popular items in one or two communities.
-- voting_clusters.sql drops popular items (--cap) and needs 10+ shared items in 2+ communities, so
-- every pair of such accounts is filtered out before it is ever scored. Here the candidates are
-- chosen by who they are instead of what they voted on:
--
--   * first seen within the window ("created" is first-seen for remote accounts)
--   * never posted or commented
--   * paired only with accounts from the SAME instance first seen within :burst_hours of them
--
-- Those pairs are then compared on ALL items, popular ones included. The caller scores each pair
-- against the same popularity-weighted null as voting_clusters, with the universe being every vote
-- in the window rather than just low-traffic ones.
WITH fresh AS (
    SELECT u.id, u.instance_id, u.created
    FROM "user" u
    WHERE u.created >= NOW() - make_interval(days => :days)
      AND u.deleted IS NOT TRUE
      AND NOT EXISTS (SELECT 1 FROM post p WHERE p.user_id = u.id)
      AND NOT EXISTS (SELECT 1 FROM post_reply r WHERE r.user_id = u.id)
),
v AS (
    SELECT pv.user_id, pv.post_id * 2 AS item, SIGN(pv.effect)::int AS dir
    FROM post_vote pv JOIN fresh f ON f.id = pv.user_id
    WHERE pv.created_at >= NOW() - make_interval(days => :days) AND pv.effect <> 0
    UNION ALL
    SELECT prv.user_id, prv.post_reply_id * 2 + 1, SIGN(prv.effect)::int
    FROM post_reply_vote prv JOIN fresh f ON f.id = prv.user_id
    WHERE prv.created_at >= NOW() - make_interval(days => :days) AND prv.effect <> 0
),
-- popularity of every item these accounts touched, counted over all voters in the window
pop AS (
    SELECT i.item, COUNT(*) AS n
    FROM (SELECT DISTINCT item FROM v) i
    JOIN post_vote pv ON MOD(i.item, 2) = 0 AND pv.post_id = i.item / 2
    WHERE pv.created_at >= NOW() - make_interval(days => :days) AND pv.effect <> 0
    GROUP BY i.item
    UNION ALL
    SELECT i.item, COUNT(*)
    FROM (SELECT DISTINCT item FROM v) i
    JOIN post_reply_vote prv ON MOD(i.item, 2) = 1 AND prv.post_reply_id = i.item / 2
    WHERE prv.created_at >= NOW() - make_interval(days => :days) AND prv.effect <> 0
    GROUP BY i.item
),
totals AS (
    SELECT v.user_id, COUNT(*) AS n, SUM(pop.n) AS s_pop
    FROM v JOIN pop USING (item)
    GROUP BY v.user_id
),
universe AS (
    SELECT (SELECT COUNT(*) FROM post_vote
            WHERE created_at >= NOW() - make_interval(days => :days) AND effect <> 0)
         + (SELECT COUNT(*) FROM post_reply_vote
            WHERE created_at >= NOW() - make_interval(days => :days) AND effect <> 0) AS v
),
pairs AS (
    SELECT a.user_id AS u1, b.user_id AS u2, COUNT(*) AS shared
    FROM v a
    JOIN fresh fa ON fa.id = a.user_id
    JOIN v b ON b.item = a.item AND b.dir = a.dir AND b.user_id > a.user_id
    JOIN fresh fb ON fb.id = b.user_id
    WHERE fa.instance_id IS NOT DISTINCT FROM fb.instance_id
      AND ABS(EXTRACT(EPOCH FROM (fa.created - fb.created))) <= :burst_hours * 3600
    GROUP BY 1, 2
    HAVING COUNT(*) >= :burst_min_shared
)
SELECT p.u1, p.u2, p.shared,
       t1.n AS n1, t2.n AS n2, t1.s_pop AS s1, t2.s_pop AS s2,
       (SELECT v FROM universe) AS universe
FROM pairs p
JOIN totals t1 ON t1.user_id = p.u1
JOIN totals t2 ON t2.user_id = p.u2
