import unittest
from unittest.mock import patch

from app.utils import dedupe_post_ids


class TestDudupePostIds(unittest.TestCase):

    @patch('app.utils.low_value_reposters')
    def test_basic_dedupe(self, mock_low_value):
        """Test basic dedupe with no cross-posts"""
        mock_low_value.return_value = set()
        params = [(1, None, 1, 10), (2, None, 1, 10)]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [1, 2])

    @patch('app.utils.low_value_reposters')
    def test_empty_input(self, mock_low_value):
        """Test with empty input"""
        mock_low_value.return_value = set()
        result = dedupe_post_ids([], True)
        self.assertEqual(result, [])

        result = dedupe_post_ids(None, True)
        self.assertEqual(result, [])

    @patch('app.utils.low_value_reposters')
    def test_regular_cross_posts(self, mock_low_value):
        """Test cross-posts from regular users - first post wins"""
        mock_low_value.return_value = set()

        params = [
            (1, [2, 3], 100, 5),  # Original post
            (2, [1, 3], 101, 10), # Cross-post 1
            (3, [1, 2], 102, 15)  # Cross-post 2
        ]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [1])  # Only first post should remain

    @patch('app.utils.low_value_reposters')
    def test_low_value_reposter_cross_posts(self, mock_low_value):
        """Test cross-posts from low-value reposters - highest reply count wins"""
        mock_low_value.return_value = {100}  # User 100 is low-value

        params = [
            (1, [2, 3], 100, 5),  # Low-value reposter post
            (2, [1, 3], 101, 10), # Cross-post 1 (10 replies)
            (3, [1, 2], 102, 15)  # Cross-post 2 (15 replies) - should win
        ]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [3])  # Post with most replies should remain

    @patch('app.utils.low_value_reposters')
    def test_mixed_scenarios(self, mock_low_value):
        """Test mix of regular posts, cross-posts, and low-value reposters"""
        mock_low_value.return_value = {200}

        params = [
            (1, None, 100, 5),     # Regular post, no cross-posts
            (2, [3, 4], 101, 8),   # Regular user with cross-posts
            (3, [2, 4], 102, 12),  # Cross-post of above
            (4, [2, 3], 103, 6),   # Cross-post of above
            (5, [6, 7], 200, 3),   # Low-value reposter
            (6, [5, 7], 201, 20),  # Cross-post 1 (20 replies) - should win
            (7, [5, 6], 202, 15)   # Cross-post 2 (15 replies)
        ]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [1, 2, 6])  # Regular post, first cross-post group, best from low-value group

    @patch('app.utils.low_value_reposters')
    def test_no_cross_posts_found(self, mock_low_value):
        """Test when cross-post IDs don't match actual posts"""
        mock_low_value.return_value = {100}

        params = [
            (1, [99], 100, 5),  # Cross-post ID 99 doesn't exist in params
            (2, None, 101, 10)
        ]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [2])  # Only post 2 should remain

    @patch('app.utils.low_value_reposters')
    def test_priority_prevents_filtering(self, mock_low_value):
        """Test that prioritized posts don't get filtered by later posts"""
        mock_low_value.return_value = {100}

        params = [
            (1, [2], 100, 5),    # Low-value reposter, prioritizes post 2
            (2, [1], 101, 10),   # Should be kept (prioritized)
            (3, [2], 102, 15)    # Should not filter out post 2 since it's prioritized
        ]
        result = dedupe_post_ids(params, True)
        self.assertEqual(result, [2, 3])  # Both 2 and 3 should remain

    @patch('app.utils.low_value_reposters')
    def test_performance_with_1000_posts(self, mock_low_value):
        """Test performance with 1000 posts including cross-posts and low-value reposters"""
        import time

        # Set up some low-value reposters
        low_value_users = set(range(900, 910))  # 10 low-value users
        mock_low_value.return_value = low_value_users

        # Generate 1000 posts with realistic cross-post references
        params = []
        for i in range(1, 1001):
            post_id = i
            user_id = 100 + (i % 50)  # 50 different users
            reply_count = i % 100  # Reply counts 0-99

            # Create cross-posts that reference other posts in the same dataset
            cross_posts = None
            if i % 10 == 0 and i <= 900:  # Every 10th post has cross-posts, but only up to 900 to avoid out-of-range
                # Create 2-3 cross-posts that reference posts within our 1000 post range
                cross_posts = [i + 50, i + 100]  # Reference posts 50 and 100 positions ahead
                if i % 20 == 0 and i <= 800:  # Some have 3 cross-posts
                    cross_posts.append(i + 150)

            # Some users are low-value reposters
            if user_id in low_value_users:
                user_id = 900 + (i % 10)  # Map to low-value users

            params.append((post_id, cross_posts, user_id, reply_count))

        # Time the execution
        start_time = time.time()
        result = dedupe_post_ids(params, True)
        end_time = time.time()

        execution_time = end_time - start_time
        print(f"\\nPerformance test: {len(params)} posts processed in {execution_time:.4f} seconds")
        print(f"Result: {len(result)} posts after deduplication")
        print(f"Deduplication ratio: {len(result)/len(params):.2%}")

        # Should complete reasonably quickly (under 1 second for 1000 posts)
        self.assertLess(execution_time, 1.0, "Function should complete within 1 second for 1000 posts")
        self.assertGreater(len(result), 0, "Should return some posts")
        self.assertLess(len(result), 1000, "Should deduplicate some posts")

    @patch('app.utils.low_value_reposters')
    def test_worst_case_performance(self, mock_low_value):
        """Test worst-case performance: many low-value reposters with many cross-posts"""
        import time

        # Make half the users low-value reposters to trigger more nested loops
        low_value_users = set(range(100, 150))  # 50 low-value users
        mock_low_value.return_value = low_value_users

        # Generate 1000 posts where many are from low-value reposters with cross-posts
        params = []
        for i in range(1, 1001):
            post_id = i
            user_id = 100 + (i % 100)  # 100 different users, half are low-value
            reply_count = i % 100  # Reply counts 0-99

            # Give every low-value reposter cross-posts to trigger nested loop
            cross_posts = None
            if user_id in low_value_users and i <= 900:
                # Each low-value post has 3-5 cross-posts that exist in our dataset
                cross_posts = [i + 10, i + 20, i + 30]
                if i <= 800:
                    cross_posts.extend([i + 40, i + 50])
            elif i % 5 == 0 and i <= 950:  # Regular users also have some cross-posts
                cross_posts = [i + 25, i + 35]

            params.append((post_id, cross_posts, user_id, reply_count))

        # Time the execution
        start_time = time.time()
        result = dedupe_post_ids(params, True)
        end_time = time.time()

        execution_time = end_time - start_time
        print(f"\\nWorst-case test: {len(params)} posts processed in {execution_time:.4f} seconds")
        print(f"Result: {len(result)} posts after deduplication")
        print(f"Deduplication ratio: {len(result)/len(params):.2%}")

        # Even in worst case, should complete in reasonable time
        self.assertLess(execution_time, 2.0, "Function should complete within 2 seconds even in worst case")
        self.assertGreater(len(result), 0, "Should return some posts")
        self.assertLess(len(result), 1000, "Should deduplicate some posts")


if __name__ == '__main__':
    unittest.main()
