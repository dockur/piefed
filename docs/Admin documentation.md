# Admin Area Settings Reference

## Site Profile - /admin/site

- **Site Name** - The display name of the instance, shown in the page title, header and federation metadata.
- **Tagline** - A short description of the instance shown alongside the site name.
- **Icon** - Upload a new site icon (jpg, jpeg, png, webp or svg); 512px and 192px versions are generated automatically for favicons and app manifests.
- **Sidebar** - Free-text (markdown) content shown in the sidebar of the home page.
- **About** - Longer "about this instance" text (markdown), typically shown on an about page.
- **Announcement at top of home page** - A markdown banner displayed at the top of the home page for all visitors.
- **Legal information** - Legal/privacy text (markdown) shown in the footer or legal page.
- **Terms of service url** - Link to an external terms-of-service page (max 255 chars).
- **General instance contact email address** - Public contact address for the instance; required.

## Misc Settings - /admin/misc

- **Enable downvotes** - Turns the downvote button on or off instance-wide.
- **Ignore comments that are just a gif reaction** - GIF-only reply comments don't decrease the author's reputation.
- **Stop local 4chan screenshot posts and notify about remote ones** - Blocks image posts that look like 4chan screenshots locally and sends a notification when a remote one arrives.
- **Filter out comments that are simply a form of "this"** - Hides low-effort "this" agreement comments.
- **Meme communities = low-quality** - Treats all meme communities as low-quality, so upvotes there don't add to reputation.
- **Allow local image posts** - Permits image posts hosted on this instance (as opposed to link-only posts).
- **Allow video file uploads** - Controls who can upload video files directly: no one, only user ID 1, admins and staff, or any user.
- **Allow NSFW communities** - Permits communities flagged NSFW to exist and be browsed (subject to country restrictions).
- **Allow NSFL communities and posts** - Permits NSFL (gore/disturbing) communities and posts.
- **Bar people from these countries from accessing NSFW and NSFL content** - Two-letter country codes, one per line; users from these countries cannot view NSFW/NSFL content.
- **Only admins can create new local communities** - Restricts community creation to admins.
- **Notify admins about reports, not just moderators** - Sends report notifications to admins in addition to moderators.
- **Require new accounts to verify their email address** - New registrations must confirm their email before they can use the account.
- **Require CAPTCHA for new account registration** - Shows a CAPTCHA on the registration form.
- **Registration mode** - Open (anyone can register), Require application (users must answer a question and be approved), or Closed (no new registrations).
- **Question to ask people applying for an account** - The question shown on the registration application form when mode is Require application.
- **Registration approved email** - Template of the email sent to users when their registration application is approved.
- **Manually approve posts by new accounts** - Posts by newly created accounts are held for moderator approval before becoming visible.
- **Provide a list of topics to subscribe to** - Shows a topic-selection step during onboarding so new users subscribe to topics.
- **Filter setup during onboarding** - Shows the local/all filter selection during onboarding.
- **Ignore registrations from these countries** - Automatically declines registrations from the listed two-letter country codes.
- **Block registrations from these referrers (one per line)** - Rejects registrations whose HTTP referrer matches one of these values.
- **Warn if new account banned from these instances** - During registration, checks whether the username/email is banned on the listed instances and warns.
- **Primary language** - The default language of the instance interface.
- **Default theme** - The theme applied to visitors who haven't chosen one.
- **Additional CSS** - Custom CSS appended to every page.
- **Additional JS** - Custom JavaScript appended to every page.
- **Trim read posts table after this many days** - Read-post history older than this many days is purged (default 180).
- **Default home filter** - The feed shown by default on the home page: Local, Popular, or All.
- **Cache remote images locally** - Downloads and serves remote images from local storage instead of hotlinking.
- **Report comments containing an em dash (—) from newly created users** - Automatically flags likely spam/AI comments that use an em dash from new accounts.
- **Limit em dash reports to just the first occurrence per user** - Only reports the first em-dash comment per user instead of every one.
- **Log ActivityPub JSON for debugging** - Writes incoming/outgoing ActivityPub payloads to the log. Use with the `LOG_ACTIVITYPUB_TO_DB` environment variable (see last section).
- **Show moderation actions publicly** - Makes the modlog visible to the public instead of only moderators.
- **Private instance - require login to browse** - Requires visitors to log in before viewing any content. Good for privacy.
- **Show Rational Discourse Toolkit in sidebar** - Displays the inoculation/Rational Discourse Toolkit block in the sidebar.
- **Allow non-admins to add remote communities** - Lets regular users add remote (federated) communities to the instance.
- **Honeypot to block bad crawlers** - Enables a hidden honeypot field/trap that silently blocks bots and scrapers.

## Close Instance - /admin/misc, bottom

- **Closing down announcement for home page** - The announcement shown when the instance is being shut down; submitting this form also sets registration to Closed and pauses federation for 10 years.

## Instance Chooser - /admin/instance_chooser

- **Enable instance chooser** - Lists this instance on the public instance-chooser so people can discover it when picking a server. Also displays instance chooser on your instance.
- **Retrieve list of instances from** - The server to fetch the canonical instance list from (defaults to piefed.social).
- **Publish instance list** - Crawls known instances and publishes this instance's own list for others to use.
- **One-sentence elevator pitch** - Short description of the instance shown in the chooser (max 90 chars).
- **Number of admins with emergency access** - How many admins can perform emergency actions, shown in the chooser.
- **Non-admins donate enough to pay for hosting** - Declares that donations cover hosting costs.
- **This instance has automated daily backups** - Declares that automated daily backups exist.

## Federation - /admin/federation

- **Federation mode** - Blocklist denies federation with the instances listed below; allowlist permits federation only with the listed instances.
- **Allow federation with these instances** - One domain per line; only used in allowlist mode.
- **Allowlist mode** - Weak only stops users adding remote communities from non-listed instances; Strong drops all activities from non-listed instances; Intense additionally drops content originally authored on non-listed instances even if relayed via an allowed one.
- **Deny federation with these instances** - One domain per line; all federation traffic with these instances is blocked in blocklist mode.
- **Auto-defederate from any instance defederated by** - Subscribe to these instances' defederation lists; any instance they defederate from is automatically blocked here too.
- **Discard all posts, comments and PMs with these phrases (one per line)** - Content containing any of these phrases is silently dropped.
- **Discard all posts and comments by users with these words in their name (one per line)** - Content from actors whose username contains any of these words is dropped.
- **Discard all posts and comments by users with these phrases in their bio (one per line)** - Content from actors whose profile bio contains any of these phrases is dropped.
- **Automatically add new remote communities** - New communities discovered on federated instances are added automatically. Join them to make future posts arrive.

## Pre-load Communities - /admin/federation/preload

- **Number of Communities to add** - How many communities to pull from the recommended list and add to the instance (default 25).

## Remote Instance Scan - /admin/federation/remote_scan

- **Remote Server** - The domain of a remote server to scan for communities.
- **Number of Communities to add** - Maximum number of communities to import from that server (default 25).
- **Communities must have at least this many posts** - Minimum post count for a community to qualify (default 100).
- **Communities must have at least this many active users in the past week** - Minimum weekly active users for a community to qualify (default 100).
- **Dry Run** - Simulates the scan and reports what would be added without adding anything.

## Import/Export Banned Lists - /admin/federation/ban_lists

- **Import Bans List Json File** - Upload a JSON file of banned instances/users exported from another instance to import those bans here.
- **Export** - Downloads the current ban lists as a JSON file.

## Edit Community - /admin/communities, edit

- **Title** - Human-readable community title.
- **Url** - The community's short name/URL slug (required).
- **Description** - Community description shown on its page.
- **Icon image** - Upload a new community icon.
- **Banner image** - Upload a new community banner.
- **Rules** - The community rules text shown to posters.
- **NSFW community** - Flags the community as NSFW.
- **Only AI-generated content** - Marks the community as exclusively for AI-generated content.
- **Banned - no new posts accepted** - Freezes the community; no new posts are accepted.
- **Only accept posts from current instance** - Restricts posting to local users only.
- **Only moderators can post** - Restricts posting to the community's moderators.
- **New moderators wanted** - Shows a "mods wanted" notice on the community.
- **Accept downvotes from** - Who can downvote in this community: everyone, nobody, community members, this instance, or trusted instances.
- **Posts can be popular** - Allows the community's posts to appear in the Popular feed.
- **Posts show in All list** - Allows the community's posts to appear in the All feed.
- **Low quality / toxic - upvotes in here don't add to reputation** - Upvotes in this community don't count towards user reputation.
- **Retain content** - How long posts/comments are kept before automatic deletion (forever up to 10 years).
- **Topic** - Assigns the community to a topic category.
- **Layout** - Default post layout for the community: list, masonry, or wide masonry.
- **Posting warning** - A warning shown to users when they create a post here.
- **Posting warning override** - Overrides the warning for specific cases (e.g. certain user groups).
- **Languages** - Languages associated with the community.
- **Override remote language setting** - Ignores the language setting inherited from a remote (federated) community.
- **Override remote AI content setting** - Ignores the AI-content flag inherited from a remote community.
- **Always show translation icon on posts** - Forces the translate button to be visible on every post.
- **Old posts can be archived** - Allows old posts in the community to be archived.

## Move Community - /admin/communities, move

- **New url** - The new local URL slug to move the community to; must not already exist.
- **Set new owner** - Transfers ownership of the community when it is moved.

## Edit Topic - /admin/topics, add/edit

- **Name** - Human-readable topic name.
- **Slug** - Short unique identifier that becomes part of the topic's URL.
- **Parent topic** - Assigns the topic under another topic, forming a hierarchy.
- **Show posts from child topics** - Includes posts from all child topics when viewing this topic.
- **Countries** - Two-letter country codes (one per line) the topic is associated with.

## Topic Import - /admin/topics, import

- **Json file that was exported from another instance** - Imports a topic tree from a JSON export produced by another instance.

## Edit Instance - /admin/instances, edit

- **Dormant** - Marks the remote instance as dormant (not actively federating).
- **Gone forever** - Marks the instance as permanently gone.
- **Trusted** - Marks the instance as trusted; its users can downvote in communities that allow trusted instances.
- **Silenced** - Mutes the instance: its content is accepted but not shown in local/all feeds.
- **Hide from instance chooser** - Excludes the instance from the public instance-chooser list.
- **Communities become popular when added** - Communities from this instance are eligible for the Popular feed as soon as they're added.
- **Posting warning** - A warning displayed to local users when posting to communities on this instance.
- **Notes about this instance (only visible to other admins)** - Internal admin-only notes.
- **Inbox** - Overrides the ActivityPub inbox URL for the instance.

## Contact Instance - /admin/instances, contact

- **Admin** - Which admin of the remote instance to address the message to. Defaults to most recently active one.
- **Message** - The message body (1–5000 chars) sent to the remote instance's admin.

## Create Offline Instance - /admin/instances, create offline

- **Domain (not including https://)** - Registers a known-but-offline instance domain locally so it can be referenced/blocked without federating with it.

## Add User - /admin/users, add

- **User name** - The new account's username; cannot contain @, cannot clash with an existing user or community.
- **Email address** - The account's email address; must be unique.
- **Password** / **Repeat password** - Initial password (min 8 chars); common or repeated-character passwords are rejected.
- **Bio** - The profile bio text.
- **Matrix User ID** - Optional Matrix handle shown on the profile.
- **Avatar image** / **Top banner image** - Optional profile images.
- **This profile is a bot** - Flags the account as a bot.
- **This profile is a reposter** - Flags the account as a reposter bot.
- **Email address is verified** - Marks the email as already verified.
- **Banned** - Creates the account in a banned state.
- **Subscribe to email newsletter** - Subscribes the account to the newsletter.
- **Posts by bots** - How to display bot posts: show, hide completely, blur, or make semi-transparent.
- **NSFW posts** / **NSFL posts** - How to display NSFW/NSFL content for this user: show, hide, blur, or semi-transparent.
- **Role** - Account role: User, Staff, or Admin.

## Edit User - /admin/users, edit

- **This profile is a bot** / **Flag their posts as from a bot** - Mark the account as a bot / force the bot label on their posts. Votes from bots are ignored.
- **This profile is a reposter** / **Flag their posts as from a reposter (bot)** - Mark as a reposter / force the reposter label on their posts.
- **Suppress cross-posts** - Hides this user's cross-posts from feeds if the viewer would have seen an alternative elsewhere in their feed.
- **Email address is verified** - Marks the user's email as verified.
- **Banned** - Bans (or unbans) the account.
- **Ban posts** / **Ban comments** / **Ban votes** - Prevents this user from posting, commenting, or voting respectively.
- **Can send PMs** - Allows or blocks the user from sending private messages.
- **NSFW posts** / **NSFL posts** - Per-user display mode for NSFW/NSFL content (show, hide, blur, semi-transparent).
- **Role** - Changes the account role between User, Staff and Admin.
- **Remove avatar** / **Remove banner** - Deletes the user's current avatar or banner image.
- **Notes about this user (only visible to other admins)** - Internal admin-only notes about the user.

## Mass Ban - /admin/users, mass ban

- **Users** - Actor IDs of the accounts to ban, one per line or separated by commas/spaces.
- **Reason** - The ban reason recorded in the modlog.
- **Delete all content by these accounts** - Purges all posts and comments by the banned accounts in addition to banning them.

## Auto Ban - /admin/users, auto ban

- **Instances to follow** - Instance domains whose ban lists are monitored, one per line.
- **Keywords in ban reason which trigger a local ban** - If a followed instance bans a user with one of these keywords/phrases in the reason, the user is automatically banned locally too.

## Newsletter - /admin/newsletter

- **Subject** - The email subject line.
- **Body (text)** - Plain-text version of the newsletter body.
- **Body (html)** - HTML version of the newsletter body.
- **Test mode** - Sends the newsletter only to the admin's own address as a test instead of all subscribers.

## CMS Page - /admin/pages, add/edit

- **URL path** - The path the page is served at (e.g. /about-us); a leading slash is added automatically and must be unique.
- **Page title** - The page's display title.
- **Content (Markdown)** - The page body, written in markdown and rendered to HTML.

## Emoji - /admin/emoji, add/edit

- **Token or character** - The emoji shortcode or single character (e.g. `:happy:` or 👍).
- **URL** - Image URL the token renders as.
- **Keywords** - Aliases/keywords that also trigger the emoji.
- **Category** - The emoji picker category.

## Emoji Filter - /admin/emoji/filter

- **Filter** - A list (max 1000 chars) of emoji tokens to block from being used on the instance.

## Add Blocked Image - /admin/blocked_images, add

- **Url** - URL of the image to block (used to compute/verify the hash).
- **Hash** - The 256-character perceptual hash of the image to block.
- **Filename** - Optional original filename, for reference.
- **Note** - Optional internal note about why the image is blocked.

## Edit Blocked Image - /admin/blocked_images, edit

- **Hash** - The 256-character image hash identifying the blocked image.
- **Filename** - Optional original filename.
- **Note** - Optional internal note about the block.

# Environment Variables

All variables are loaded from the `.env` file in the project root (see `config.py`, `env.sample` and `INSTALL.md`). 
Copy `env.sample` to `.env` and edit it to suit your server. Each entry lists the type as parsed in `config.py`: `string`, `int`, `float`, or `flag` (a string such as `'1'`/`'true'`/`'True'` interpreted as a boolean; for some flags, like `MAIL_USE_TLS`, the presence of the variable is enough to enable the feature).

## Core

- **SERVER_NAME** (string) - The public domain of the instance (lowercase, e.g. `piefed.social`); used to build URLs and the default `MAIL_FROM` address. Defaults to `localhost`.
- **SECRET_KEY** (string) - Flask session-signing secret; must be set to a random sequence of numbers and letters.
- **DATABASE_URL** (string) - SQLAlchemy connection string for the database, e.g. `postgresql+psycopg2://user:pass@localhost/pyfedi`; falls back to a local sqlite file.
- **JWT_EXPIRY_DAYS** (int) - How many days API JWT tokens remain valid (default 365).
- **HTTP_PROTOCOL** (string) - `https` by default; set to `http` during local development so features work without SSL.
- **FLASK_APP** (string) - Entry point module name (`pyfedi.py`), used by the Flask CLI.
- **FLASK_DEBUG** (string) - Set to `1` to enable the `/dev/tools` debugging page (development only).

## Email

- **MAIL_SERVER** (string) / **MAIL_PORT** (int) / **MAIL_USE_TLS** (flag, presence-based) / **MAIL_USERNAME** (string) / **MAIL_PASSWORD** (string) - SMTP settings for outgoing mail; leave `MAIL_SERVER` empty to send via AWS SES instead. `MAIL_USE_TLS` is enabled by the mere presence of the variable. Test with `/test_email`.
- **MAIL_FROM** (string) - The sender address for outgoing mail; defaults to `noreply@<SERVER_NAME>`.
- **ERRORS_TO** (string) - An email address that receives error reports (only useful with `MAIL_*` set, for debugging).
- **BOUNCE_ADDRESS** (string) / **BOUNCE_HOST** (string) / **BOUNCE_HOST_TYPE** (string) / **BOUNCE_USERNAME** (string) / **BOUNCE_PASSWORD** (string) - IMAP/POP3 mailbox that bounced emails go to; the app reads and deletes everything in it, so use a dedicated inbox.

## Cache, Redis and Celery

- **CACHE_TYPE** (string) - `FileSystemCache` (fine for development) or `RedisCache` (recommended in production).
- **CACHE_DIR** (string) - Directory for the file-system cache, e.g. `/dev/shm/pyfedi`.
- **CACHE_REDIS_URL** (string) - Redis URL for the cache when using `RedisCache` (default db 1); check `/testredis` to verify.
- **CELERY_BROKER_URL** (string) / **RESULT_BACKEND** (string) - Redis URLs for the Celery task queue and results (default db 0).
- **REDIS_MEMORY_LIMIT** (int) - Federation pauses once Redis uses this much memory in bytes (default 200 MB); `-1` disables the check.

## Error tracking and geolocation

- **SENTRY_DSN** (string) - Sentry DSN for error monitoring; empty disables it.
- **AWS_REGION** (string) - AWS region used for SES email sending and S3-compatible storage.
- **IPINFO_TOKEN** (string) - API token for the ipinfo.io geolocation service, used to determine visitors' countries.
- **COUNTRY_SOURCE_HEADER** (string) - Name of an HTTP header (e.g. `CF-IPCountry` from Cloudflare) that carries the country code; when set it is used instead of IPInfo.
- **CLOUDFLARE_API_TOKEN** (string) / **CLOUDFLARE_ZONE_ID** (string) - Cloudflare API credentials, e.g. for purging cache or managing DNS.

## Media, uploads and image processing

- **MEDIA_IMAGE_MAX_DIMENSION** (int) - Images are resized so neither side exceeds this many pixels (default 2000).
- **MEDIA_IMAGE_FORMAT** (string) / **MEDIA_IMAGE_QUALITY** (int) - Output format (empty = keep original) and quality for full-size images (default 90).
- **MEDIA_IMAGE_MEDIUM_FORMAT** (string) / **MEDIA_IMAGE_MEDIUM_QUALITY** (int) - Format and quality for medium-sized images (default WEBP 93).
- **MEDIA_IMAGE_THUMBNAIL_FORMAT** (string) / **MEDIA_IMAGE_THUMBNAIL_QUALITY** (int) - Format and quality for thumbnails (default WEBP 93).
- **FILE_UPLOAD_QUOTA** (int) - Per-upload size limit in bytes for images inserted into post/comment bodies (default 50 MB); not applied to image posts.
- **IMAGE_HASHING_ENDPOINT** (string) - URL of an external image-hashing service used for blocked-image detection.
- **CHAN_DETECTION_ENDPOINT** (string) - URL of an external service that detects 4chan-screenshot images (see the chan image filter in Misc settings).

## S3 storage

- **S3_REGION** (string) / **S3_ENDPOINT** (string) / **S3_BUCKET** (string) - Region, endpoint URL (for S3-compatible services) and bucket name for object storage.
- **S3_ACCESS_KEY** (string) / **S3_ACCESS_SECRET** (string) - Credentials for the bucket.
- **S3_PUBLIC_URL** (string) - Public base URL for files served from the bucket.
- **S3_PUBLIC_ACL** (flag) - Whether uploaded objects get a public-read ACL.
- **S3_STORAGE_CLASS** (string) - Optional storage class for uploads (e.g. for archiving old posts).

## Content retention and archiving

- **KEEP_LOCAL_VOTE_DATA_TIME** (int) / **KEEP_REMOTE_VOTE_DATA_TIME** (int) - Months to keep local/remote voting data before deletion; `-1` means never delete (default 6).
- **ARCHIVE_POSTS** (int) - Posts older than this many months get their post+replies rendered to JSON, stored to S3/disk and deleted from the DB; `0` disables.
- **DEFAULT_CONTENT_RETENTION** (int) - Default content retention for remote communities in months (`-1` = forever).
- **BOT_CONTENT_RETENTION** (int) - Months to keep bot content that isn't stickied and has no replies (`-1` = forever, default 6).
- **MICROBLOG_CONTENT_RETENTION** (int) - Months to keep microblog content (`-1` = forever).
- **CONTENT_WARNING** (int) - Global content-warning flag applied to posts.

## Ranking and anti-spam

- **SPICY_UNDER_10** (float) / **SPICY_UNDER_30** (float) / **SPICY_UNDER_60** (float) - Multipliers that amplify the effect of early votes in the "hot" sort's spicy score: the first 10 total votes get the first multiplier, votes up to 30 the second, and so on (all default 1.0, i.e. no amplification).
- **VOTE_QUOTA** (int) - Maximum votes a user can cast per day (default 240).
- **ANOOBIS** (flag) / **ANOOBIS_DIFFICULTY_DESKTOP** (int) / **ANOOBIS_DIFFICULTY_MOBILE** (int) - Enables a proof-of-work challenge for anonymous visitors to make scraping harder, with separate difficulty levels for desktop (default 19) and mobile (default 12).
- **FLAG_THROWAWAY_EMAILS** (flag) - Flags registrations using disposable/throwaway email addresses.
- **ALLOW_AI_CRAWLERS** (flag) - Permits known AI crawler user-agents to access the instance.
- **RSS_FEEDS** (flag) - Allows people to add RSS feeds to communities.

## API, rate limiting and webhooks

- **ENABLE_ALPHA_API** (flag) - Enables the alpha API for mobile apps (set to the string `'true'`).
- **SERVE_API_DOCS** (flag) - Serves the API documentation pages.
- **CORS_ALLOW_ORIGIN** (string) - Allowed origin for API CORS requests; defaults to `*` (any origin), set to a specific domain for security.
- **RATELIMIT_ENABLED** (flag) - Enables rate limiting (default true).
- **SKIP_RATE_LIMIT_IPS** (string) - List of IPs exempt from rate limiting (default `['127.0.0.1']`).
- **ENABLE_WEBHOOK_ROUTE** (flag) - Enables the webhook endpoint (used e.g. by Stripe).
- **PAGE_LENGTH** (int) - Number of items per page in listings (default 100).

## OAuth logins

- **GOOGLE_OAUTH_CLIENT_ID** (string) / **GOOGLE_OAUTH_SECRET** (string) - Google OAuth credentials for "log in with Google".
- **MASTODON_OAUTH_CLIENT_ID** (string) / **MASTODON_OAUTH_SECRET** (string) / **MASTODON_OAUTH_DOMAIN** (string) - Mastodon OAuth credentials and the domain of the Mastodon instance to authenticate against.
- **DISCORD_OAUTH_CLIENT_ID** (string) / **DISCORD_OAUTH_SECRET** (string) - Discord OAuth credentials.

## Payments (Stripe)

- **STRIPE_SECRET_KEY** (string) / **STRIPE_PUBLISHABLE_KEY** (string) - Stripe API keys from the Stripe dashboard.
- **STRIPE_MONTHLY_SMALL** (string) / **STRIPE_MONTHLY_BIG** (string) - Price IDs of the two monthly recurring donation products.
- **STRIPE_MONTHLY_SMALL_TEXT** (string) / **STRIPE_MONTHLY_BIG_TEXT** (string) - Display text for those two donation tiers (e.g. "Pay $5 per month").
- **WEBHOOK_SIGNING_SECRET** (string) - Signing secret of the Stripe webhook that posts to `/stripe_webhook`.

## LDAP

- **LDAP_SERVER** (string) / **LDAP_PORT** (int) / **LDAP_USE_SSL** (flag) / **LDAP_USE_TLS** (flag) / **LDAP_BASE_DN** (string) - Connection settings for the LDAP server.
- **LDAP_WRITE_ENABLE** (flag) / **LDAP_WRITE_BIND_DN** (string) / **LDAP_WRITE_BIND_PASSWORD** (string) / **LDAP_WRITE_USER_FILTER** (string) / **LDAP_WRITE_ATTR_USERNAME** (string) / **LDAP_WRITE_ATTR_EMAIL** (string) / **LDAP_WRITE_ATTR_PASSWORD** (string) - "Write" mode: this instance pushes new account details to the LDAP server so other services can use their instance credentials to log in here.
- **LDAP_READ_ENABLE** (flag) / **LDAP_READ_USER_FILTER** (string) / **LDAP_READ_ATTR_USERNAME** (string) / **LDAP_READ_ATTR_EMAIL** (string) - "Read" mode: this instance authenticates logins against the LDAP server.

## External services

- **NOTIF_SERVER** (string) - URL of the companion piefed-notifs service (see INSTALL.md) for real-time push notifications.
- **TRANSLATE_ENDPOINT** (string) / **TRANSLATE_KEY** (string) - Endpoint and key of a LibreTranslate-compatible service for the post translation feature.
- **DETECT_AI_ENDPOINT** (string) - Endpoint of an AI-content detection service.
- **FULL_AP_CONTEXT** (flag, `'0'`/`'1'`) - When `1`, serves the full ActivityPub context in responses.
- **LOG_ACTIVITYPUB_TO_DB** (flag) / **LOG_ACTIVITYPUB_TO_FILE** (flag) - Log ActivityPub payloads to the database or a file for debugging.

## Database pool

- **DB_POOL_SIZE** (string, numeric) / **DB_MAX_OVERFLOW** (string, numeric) - SQLAlchemy connection pool size and max overflow (defaults 10 and 30); read without an int cast, so supplied as strings.
- **NUM_CPU** (int) - Number of CPUs to use; `0` means auto-detect from the core count.

## Session cookies

- **SESSION_COOKIE_SECURE** (flag) - Marks the session cookie Secure (default on).
- **SESSION_COOKIE_HTTPONLY** (flag) - Marks the session cookie HttpOnly (default on).


