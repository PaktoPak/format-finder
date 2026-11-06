# format-finder design

This is the contract every module follows. Read it before changing code.

## What the tool does

Input: an app brief. Output: ten short-form video formats worth testing, each backed by outlier videos.
An outlier is a video with far more views than its own account normally gets.

Claude does the judgment steps (reading the brief, writing search phrases, grouping videos into formats,
checking fit, writing the ten). The `format-finder` command-line tool does the data steps (search,
account history, outlier maths, downloads, frames, transcripts). Every command prints JSON to stdout.
Logs go to stderr.

Rules that apply everywhere:
- Views are the success measure. Nothing in the tool measures signups, downloads or conversion.
- No hashtag search, anywhere. Search is by keyword only.
- Everything is free. No paid APIs, no paid keys.
- Nothing carries over between runs except the Instagram login session, the settings file and the browser
  route's own profile (`browser-profile/`, which keeps what sites store in it, and a login if you made one there).

## Where things live

- Plugin code: `plugins/format-finder/` (this repo). `src/format_finder/` is the Python package.
- User data: `$FORMAT_FINDER_HOME`, default `~/.format-finder/`:
  - `venv/` Python environment (created by `scripts/setup.sh`)
  - `vendor/tiktok-scraper/` omkarcloud tiktok-scraper, pinned commit, imported as a library
  - `node/` YouTube.js (`node_modules`) plus `yt.mjs`, copied from the plugin by setup
  - `settings.json` user overrides (created empty by setup; `defaults/settings.json` holds every default)
  - `instagram_session.json` Instagram login session (created by `format-finder instagram-login`)
  - `models/` Whisper models
  - `browser/` Chrome for Testing, only when setup found no browser and downloaded one
  - `browser-profile/` the browser route's own profile (never your everyday browser profile), with
    `browser-profile.lock` / `.owner` (one command at a time) and `browser.log` (the browser's own messages)
- Run output: whatever `--out` directory the skill passes, normally `./format-finder-runs/<timestamp>/`.

## Routes per platform (two free routes each, plus fallbacks)

| Platform | Route 1 | Route 2 | Fallbacks, in order |
|---|---|---|---|
| YouTube | yt-dlp | YouTube.js (Node) | none |
| TikTok | omkarcloud tiktok-scraper (library import) | Douyin_TikTok_Download_API (REST, user runs it in Docker) | yt-dlp (account, video, download; no search, no followers), then the browser route (search, account, video; see below) |
| Instagram | instagrapi (logged-in session) | aiograpi (same session) | the browser route (account, video, download, and search only when `instagram_browser_search` is true; needs a login the person made in the browser profile; see below) |
| App stores | Apple iTunes Search API | none | none |

`apps` keeps each app's `developer_url` (iTunes `sellerUrl`). With `--links` it reads each developer website once
(plain GET, no cookies, the first 2 MB) and lists the TikTok (`tiktok.com/@...`), Instagram (`instagram.com/<name>` or
`instagram.com/_u/<name>` on instagram.com itself, never another subdomain, a file such as `embed.js`, or post, reel,
explore, tag, embed or static pages) and YouTube profile links on it as `social_links` (`@handle`, or
`youtube.com/c/...`, `/user/...`, `/channel/...` paths, which `account youtube` opens as given). A site that cannot be
read gives that app `social_links: {"error": ...}` and nothing else is lost. Live on 2026-11-05 (one search phrase, 12 apps, 19 s) the sites linked handles that differ from the ones a test
run had guessed (and found empty or missing): one TikTok and four Instagram handles.

`common.try_routes` runs routes in order and moves on when a route raises or returns an empty result. It stops at
`common.NotFound`: the platform has answered that the account or video does not exist, so the later routes are not
asked. A route that cannot tell a missing account from an answer with no data in it raises
`NotFound(final=False)`: the later routes still ask, and it does not count as a route failure.

### Route memory ("fastest working route")

Platform modules call `try_routes(..., _op="tiktok.search")` (one name per platform and operation; `_op` is
not passed to the routes). Within one process, which for a scan means one run:
- A route that raised on 2 calls of that operation in a row (`common.ROUTE_FAILS_TO_SKIP`, a fixed limit in the code, not in the settings file) is
  skipped for the rest of the process while another route of the operation is still in use. When every route
  of the operation is skipped, the ones skipped for failing are tried again; a result puts a route back in use.
- A route whose error has `captcha = True` (`browser.BotCheck` for a captcha or puzzle) is skipped for that
  operation at once and is not tried again in that process: one captcha means more would follow.
- A result resets the count. An empty result neither counts nor resets. `NotConfigured` counts as a failure;
  `NotFound` does not.
- Other operations are not affected: a captcha on `tiktok.search` leaves `tiktok.account` and `tiktok.video`
  on the browser route.
- Each skip is logged once on stderr (`[routes] ...`). The envelope gets `skipped_routes` (names) when a route
  was skipped; when every route was skipped, `AllRoutesFailed` carries `RouteSkipped: <reason>` errors.
  `common.route_skips()` lists every skip: `{op, route, captcha, reason, failed_calls, skipped_calls, active}`.

## Normalized records

All records are plain dicts built with `common.make_video` / `common.make_account`. Unknown values are `None`,
never guessed.

Video:
```
platform, id, url, caption, author_id, author_username, author_url, author_followers,
views, likes, comments, shares, posted_at (ISO 8601 UTC or None), duration_s,
sound_id, sound_title, sound_author, is_paid (True/False/None), route, raw_ref
```
`raw_ref` is whatever the route needs to fetch the item again (id, url, code).

Account:
```
platform, id, username, url, followers, post_count, is_private, posts (list of Video, newest first), route
```
`post_count` and `is_private` are what the profile says (None when the route does not read them). When every route
that answered returned no posts and no followers, the envelope has `empty: true`: a missing, private or blocked
account looks the same there. An account that came back with followers but no posts is a result (`try_routes`
asks no later route); the CLI marks it `empty: true` too, since it has nothing to score, and adds `empty_reason` to
both (for Instagram: the Reels tab gave none).

## Platform module interface

Each of `youtube.py`, `tiktok.py`, `instagram.py` exposes exactly these functions. Each returns an
envelope dict `{"route": name or None, "errors": [ {route, error} ], "result": ...}` built by `try_routes`
(plus `skipped_routes` when route memory skipped one; YouTube search adds `window`).

- `search(query, days=None, limit=30, region=None)` → result: list of Video
- `account(handle, limit=20)` → result: Account (posts newest first)
- `video(ref)` → result: Video
- `sound(ref, limit=30)` → result: list of Video using that sound
- `download(ref, out_dir)` → result: `{"video_path": str or None, "captions_text": str or None, "captions_source": str or None}`
  (both YouTube routes add `captions_language` and `captions_language_checked`: whether that language was matched
  against the original audio language YouTube reports; `yt.mjs` returns them too, and a reply from an older copy
  without them counts as unchecked)

If a route cannot run at all (no login, service not running, dependency missing) it raises
`common.NotConfigured` with a message that says what the user must do. When the platform says the account or
video does not exist, it raises `common.NotFound`; route memory and the scan's skip rule do not count that as a
route failure, and `try_routes` asks no later route (after a `final=False` one it does). Routes that raise it: Instagram's session routes (the libraries'
`UserNotFound` and `MediaNotFound`) and its browser route (a missing profile page); TikTok's route 1 (the vendored
scraper's own `TikTokNotFound` for a user, which it also gives for a profile TikTok keeps hidden and for an answer
with no profile in it, as a blocked page gives, so it is `final=False`) and its browser
route (user-detail status 10202, 10221 or 10223). TikTok's status 10222 (a profile it keeps hidden, "secret") gives a
private account with no posts. YouTube and TikTok's other routes raise no `NotFound`.

Messages that tell the user to run a command in their own terminal (`instagram-login`, `browser-login`) give
`common.TERMINAL_COMMAND`, the launcher's full path (quoted when it has spaces), since the plugin's `bin/` is on the
PATH only inside Claude Code; doctor prints it as `terminal_command`.

## Media (`media.py`)

- `media` prints, besides the frames: `transcript`, `transcript_source`, `transcript_language`, and for a Whisper
  transcript `language_probability`, `segments` with `avg_logprob` and `no_speech_prob`, `speech` (false when
  Whisper heard nothing) and `low_confidence` (true when the segments' mean `avg_logprob` is under -1.0 or their
  mean `no_speech_prob` is over 0.6, Whisper's own defaults for text it should not trust; music-only Shorts gave
  short repeated lines such as "I'm sorry." that differed from run to run).
- `captions_language_checked` comes from the download: true when the caption language matched the audio language
  YouTube reports, false when it was not checked, null when the route does not say and for a Whisper transcript.
  Captions whose language was not checked get Whisper's language detection too, when Whisper is on:
  `audio_language`, `audio_language_probability` and `language_mismatch` (absent when the detection did not run). On
  one test video (2026-11-05) the only original-language caption track was Ukrainian while Whisper heard Russian
  (0.556). YouTube's codes `iw`, `jv` and `fil` are compared as Whisper's `he`, `jw` and `tl`.
- ffmpeg, and YouTube.js's Node process, run with stdin closed (`subprocess.DEVNULL`): ffmpeg reads keys from stdin
  while it runs, and in a shell `while read` loop over a list of videos it ate the start of the next line.

## Browser route (`browser.py`, TikTok's and Instagram's `_browser_*` routes)

The last TikTok and Instagram route for `search`, `account` and `video`, and Instagram's last `download` route (never
`sound`; Instagram search only when `instagram_browser_search` is true). It opens the
site's own pages in a headless Chromium-family browser and reads what the page itself loads, over the DevTools
protocol (the `websockets` package). The page's scripts sign their own requests. The tool types nothing into a page
and never prints or stores a cookie, password or token, and never looks one up (for Instagram, see the note on
`/api/graphql` below: a reply with an encrypted value reaches the route's memory and is ignored; embedded JSON read with
`JsonScripts(marker, keys)` is cut down to those keys inside the page, so Instagram's config script with its csrf and
DTSG tokens is not read). The `browser_route` setting (false) removes it everywhere.

- **Which browser:** the `browser_path` setting; else Chrome for Testing that setup downloaded into
  `FF_HOME/browser`; else installed Google Chrome, Chromium, Brave, Microsoft Edge; else Chrome for Testing in
  the Playwright cache. `python -m format_finder.browser` prints the one it would use (setup.sh calls it).
  `FORMAT_FINDER_ONLY_DOWNLOADED_BROWSER=1` makes it look only in `FF_HOME/browser` and `browser_path` (to
  test setup's download).
- **One browser per process**, started on first use with the dedicated profile `FF_HOME/browser-profile`,
  reused for every page, closed with all its helper processes when the process ends (also on SIGTERM/SIGHUP).
  A lock file lets one format-finder command use the profile at a time; a second one waits 20 s, then fails
  naming the first.
- **Headless flags** include `--disable-renderer-backgrounding`, `--disable-background-timer-throttling` and
  `--disable-backgrounding-occluded-windows`, and the tab is brought to front. Tested on one Mac (Brave 154,
  2026-11-05): without them the page started "hidden" in 4 of 6 launches, drew no frames, and the scroll that
  makes infinite lists load more was never answered; with them it was visible in 5 of 5.
- **Reading a page** (`Tab.capture`): replies whose URL path matches a pattern (`/api/search/item/full/`,
  `/api/post/item_list/`, Instagram's `/api/graphql`) and the page's embedded JSON: `<script id=...>`
  (TikTok's `__UNIVERSAL_DATA_FOR_REHYDRATION__`) or, with `browser.JsonScripts(marker, keys)`, from every
  `<script type="application/json">` whose text contains the marker, only `{key: value}` for each of `keys` found in
  it, picked inside the page; a part that contains `"csrf_token"`, `"DTSGInitialData"` or `"fb_dtsg"` is dropped there
  too (Instagram). A page that loads a new
  document in its own tab is read from that document: on 2026-11-05 a TikTok profile page first came without
  data and reloaded itself about a second later. When no wanted reply arrives for 4 s, the page is checked for
  a captcha, puzzle or login wall, then scrolled (search: up to 10 result pages; profile: up to 8 post-list
  pages, `request_delay_seconds` apart).
- **Blocked pages** raise `browser.BotCheck`. `captcha = True` when TikTok's `bdturing-verify` header comes with
  an empty reply, or the page shows a captcha element or puzzle text ("Drag the slider to fit the puzzle").
  An empty search reply or a login wall is a `BotCheck` with `captcha = False` (route memory counts it as a
  failure). The tool never solves, bypasses or automates a captcha or puzzle.
- **What TikTok did from one IP** (one Mac, logged out): the first headless search from a fresh
  profile got the full search reply (12 videos a page with plays, likes, comments, shares, createTime, duration,
  music, author, followerCount, isAd, has_more, cursor). After repeated test searches, every search from that IP
  got a slider puzzle or an empty reply, with Brave or Chrome for Testing, saved or new profile. On 2026-11-05
  profile pages showed the slider puzzle when their post list loaded (the embedded profile still gave exact
  followers), while video pages read in 3-4 s with views, likes, comments, shares and date matching route 1;
  their follower count is rounded (180600 where route 1 gave 180582). Not known: how long the flag lasts, and
  whether a logged-in session in the dedicated profile avoids it (untested).
- **`format-finder browser-login tiktok|instagram`** opens a visible window of the dedicated profile at the
  site's login page and waits until the person closes it (15 minutes at most). The person logs in, or completes
  a puzzle, themselves. Whether that clears TikTok's puzzle for the headless route is untested. The Instagram
  browser route needs that login (see below).
- Timings are settings: `browser_launch_timeout_seconds`, `browser_load_timeout_seconds`,
  `browser_search_timeout_seconds`, `browser_page_timeout_seconds`.

### Instagram through the browser (`instagram._browser_*`)

Last in the Instagram route lists for account and video, after instagrapi and aiograpi, and for search only when the
`instagram_browser_search` setting is true (false by default: the keyword search page sends its own tag-information
request, see `/api/graphql` below; while it is false, search's browser route raises `NotConfigured` saying so and
opens nothing). It needs a login the person made themselves in
the dedicated profile (`format-finder browser-login instagram`); the tool never logs in. What instagram.com loaded on
2026-11-05 (one Mac, Brave, logged in), and what the route reads:
- **search** opens `/explore/search/keyword/?q=<keywords>` and reads `/api/graphql` replies with
  `data.xdt_fbsearch__top_serp_graphql` (24 media per reply in `edges[].node.items[]`, `page_info.has_next_page`).
  Each media has `pk`, `code`, `taken_at`, `media_type`, `like_count`, `comment_count`, `caption`, `user` and a DASH
  manifest (duration), but `view_count` is null and there is no `play_count`: **web search gives no views**, so
  `views` stays None. Kept: `media_type` 2 only, not marked as an ad the way the app API marks one (`_is_ad`:
  `product_type` "ad" or `ad_id`; web search items have no `product_type` and the recordings showed no ad, so this
  is untested on web search), inside `days` by
  `taken_at`. It scrolls for more (one reply per scroll; at most 8 replies, `BROWSER_SEARCH_PAGES`) until `limit` or
  no next page. Links are `/reel/<code>/`.
- **account** opens `/<username>/reels/` and reads `data.fetch__XDTUserDict.clips_connection` replies (12 reels,
  then 3 per reply; at most 8 replies, `BROWSER_REEL_PAGES`) and the `data.user` reply whose `username` matches
  (`pk`, `follower_count`, `is_private`); `data.viewer` is the logged-in person and is never used. Reels carry
  `play_count` (views), `like_count`, `comment_count`, but **no `taken_at` and no caption**, so `posted_at` and
  `caption` are None (the reel page has both) and posts keep
  the grid order (newest first). Pinned reels list the profile's id in `clips_tab_pinned_user_ids` and are left
  out; so are reels whose `user` is another account. The scan places these undated reels in its `recent_days` window by
  the creation time their media pk encodes (`instagram.time_from_pk`: `(pk >> 23)` ms after 1314220021721 ms; 10 s to
  7 min before `taken_at` on the 12 reels of the 2026-11-05 scan), for that check only; `posted_at` stays None. When
  no Reels-tab reply comes, or it carries a GraphQL error (`fetch__XDTUserDict` null), the route raises `RouteError`
  with that error even if the profile reply came. An empty post list is returned only for a private profile the
  viewer does not follow (`is_private`, not `following`) or a Reels-tab reply with no reels and no next page
  (unit-tested; seen live once on 2026-11-05: a profile with 300 posts and no reels gave its follower and post
  counts and no reels).
- **video** opens `/reel/<code>/` (a media pk is turned into its code; share and audio links are left to the
  session routes) and reads `data.xig_fetch_media` from the page's `<script type="application/json">` documents
  (only the `xig_fetch_media` and `xdt_api__v1__clips__home__connection_v2` parts, picked inside the page: in the
  2026-11-05 recording another script with the reel's code, the page config, held a csrf token and a DTSG token):
  `taken_at`, likes, comments, caption, author, `clips_metadata` (sound) and the DASH manifest
  (`mediaPresentationDuration`, the duration). No `play_count`, so `views` is None.
- **download** opens the same reel page and saves the smallest of its `video_versions` (progressive MP4 with sound
  on Instagram's CDN) that is at least 480 px wide, else the smallest (a file that lists no height comes after the
  ones that list both sizes), fetched with plain urllib (certifi's CA
  bundle when installed) and no cookie. The signed file link is never logged or printed; errors give only the HTTP
  status or the error type. Tested live on 2026-11-05 on one reel (720x1280 was the only size listed), 2.1 MB,
  `media instagram` with 8 frames and a Whisper transcript in 13 s.
- **sound** has no browser route (nothing recorded to build it from).
- **missing profile:** for a username Instagram does not have, the profile page loads other GraphQL replies (inbox,
  feed, notifications) but no `data.user`, and shows Instagram's "page isn't available" notice. At the first quiet
  moment (`browser.Tab.capture(on_idle=...)`, before any scroll) the route runs `PROFILE_MISSING_JS` (the notice's
  English words, or in any language its shape: a `<main>` with a link to `/`, no profile `<header>`, no
  `og:title` tag) and raises `NotFound`. Live on 2026-11-05 (the tool's browser profile showed the page in French):
  two handles Instagram does not have answered NotFound in 11 s and 12 s (45-48 s of scrolling before); the
  brand's real handle, from its website, gave 19 reels in 27 s.
- Profiles also give `post_count` (`media_count`) and `is_private` for the account record.
- A page that ends on Instagram's login page (`/accounts/login/`, `/challenge/`) raises `NotConfigured`: "Instagram
  needs a login in the tool's browser: run format-finder browser-login instagram in your own terminal and log in
  yourself (or use format-finder instagram-login for the faster routes)". Unit-tested only: the profile was logged
  in for every live check.
- Views for a search result come from the account history: `outliers.score` fills a candidate's missing views,
  likes and comments from the post with the same id (the media pk, a string on both pages) in that account's
  record. A result that is not among the account's newest `baseline_posts` reels keeps `views` None (`no_views`).
- **`/api/graphql`:** every query of an Instagram page goes to that one address, so the route receives the body of
  every GraphQL reply a search or profile page loads and keeps only the three kinds above. On 2026-11-05 those replies included
  one named `xdt_api__v1__web__accounts__get_encrypted_credentials` (one encrypted value); the route receives it in
  memory with the rest and ignores it, and never prints or stores it. The keyword search page itself also asked
  `fetch__XDTTagInfo` (the page's own request for tag information about the keyword; it answered null); the route
  does not ask for it and ignores the reply. Because the page sends it, browser search is off unless
  `instagram_browser_search` is true.
- Tested live on 2026-11-05 through the CLI (one Mac, logged in, browser search then on): search "meal prep", 60 days, limit 20: 20 videos
  in 27 s; one of their accounts, limit 20: followers and 20 reels with views in 18 s; one of its reels: date,
  likes, comments, sound and duration in 5 s (again in 5.5 s with the in-page selection of `JsonScripts` keys); an Instagram-only scan (1 phrase, 12 results, 4 accounts) in 92 s.
  Untested: a logged-out profile, a private profile, an account with collab reels, ads in
  web search, the media id's time for a reel saved as a draft or scheduled, pages after a login expires, and how many
  pages Instagram serves before it limits an account.

## Outlier maths (`outliers.py`)

- Account normal = median views of the account's recent posts, excluding the candidate itself and posts the
  platform marks as paid (`is_paid` True: a TikTok ad, an Instagram ad or paid partnership), whose views may be
  bought; `account_paid_posts` says how many were left out. A candidate's unknown views, likes and comments, and its
  `is_paid`, are filled from the same post in the account history (`is_paid` True from either copy wins, since some
  routes do not report ads).
- Multiple = candidate views / account normal. Recent normal = median of the `recent_normal_posts` unpaid posts the
  account made just before the candidate: the posts after it in the account's list (accounts list posts newest
  first), or, when it is not in the list, the posts with an earlier `posted_at` when both dates are known. A
  candidate missing from the list with no dates to place it is older than every listed post (the list holds the
  newest ones), so no post before it is known. With fewer than `recent_normal_posts` posts before it, the recent
  normal is null. `recent_multiple` = views / recent normal.
- Engagement rate = (likes + comments) / views; an unknown comment count counts as 0 (yt-dlp leaves it empty on
  Shorts with no comments), unknown likes leave the rate unknown. `boost_checked` says whether the boost check ran.
- Flags: `account_not_looked_up` (no history was asked for: over `max_accounts_per_run`, or no author, so the video
  was never scored), `account_lookup_failed` (the history was asked for and came back from no route, or the scan had
  stopped that lookup after failures), `too_few_posts` (a history came back with fewer than `baseline_min_posts`
  unpaid posts with views),
  `no_views`, `low_views` (views under `outlier_views_min`), `paid`, `suspected_boost` (likes+comments per view
  below `boost_ratio` x the median of the same platform's candidates), `rising_account` (meets the multiple against
  the account normal but not against the recent normal), `meets_multiple` (multiple >= `outlier_multiple_min`),
  `top_fraction` (in the top `top_fraction` of the batch by multiple; the find-formats skill looks at these first
  when it picks videos to watch). `outliers.DROP_FLAGS` are the flags that keep a video from counting as an
  outlier.
- Formats (grouped by Claude) need videos from at least `min_accounts_per_format` different accounts.
  `check-formats` counts only videos that are outliers without a drop flag (it also reads `is_paid` and the view
  floor from the record itself, for records scored before a flag existed) and lists the rest as `not_counted`.
  It lists `shared_captions`: one entry per set of accounts whose captions share runs of 6 words (hashtags and
  @mentions left out), with those runs as `phrases` (overlapping runs joined) and `accounts_if_one_producer`, the
  count when that set alone counts once; sets are not joined through each other, so a common call to action is
  judged apart from a show's name. `accounts_after_shared_captions` is the lowest count, every set counted once.
  Videos without a caption (Instagram Reels-tab posts that had no detail lookup) cannot share one. It warns when one
  username posts on two platforms, and when two counted videos on two platforms under different names are within
  1 s in length (`SAME_LENGTH_S`; YouTube gives whole seconds) and, when both dates are known, 7 days in date
  (`SAME_POST_DAYS`): possibly one video one producer posted twice. It reports the median multiple per video and
  per account and the median views. The fading check needs 4 counted videos with a date (`min_dated_videos`,
  `FADING_MIN_DATED`) whose dates span `fading_min_span_days`; `too_recent` is true when two or more dates span
  less, whatever their number, and then no halves are compared.

## Scan (`scan.py`)

- Order: provided videos (`video()`), competitor accounts (`account()`; their posts inside `recent_days`, or
  without a known time, become candidates; an Instagram post without `posted_at` is placed by its media id's time,
  see the Instagram browser route), keyword search per phrase and platform, account histories (first accounts with a result
  that can pass `outlier_views_min` or whose views are not known yet, then those with the most candidates, ties to
  the higher-placed search result, capped by `max_accounts_per_run`; a progress line goes to stderr every 20
  lookups), then `video()` details for up to `max_details_per_run` top outliers above the view floor that lack a
  date or likes/comments (YouTube Shorts search gives neither); their exact views and follower counts replace the
  rounded ones search lists show. Candidates then known to be older than `recent_days` are dropped, and everything
  is scored again.
- `--accounts-from <path>` (repeatable) reuses account histories fetched before instead of asking again: a run
  folder or its `accounts.json`, or the JSON `format-finder account` prints (a file, or a folder of them without an
  `accounts.json`; the skill saves its competitor checks in `<run>/account_checks/`). They are matched by account key,
  or for competitor accounts by handle or username; they do not count toward the cap, and their rows in
  `accounts.json` carry `reused_from`. An account with no posts is reused only when it came back with a follower or
  post count (the profile answered); one with neither may have been blocked, so it is asked again. Follow-up scans go in their own folder
  (`<run>/followup-N`), since a scan always writes `candidates.json`, `accounts.json` and `run_log.json` into `--out`.
- Each candidate carries `phrase`, `distance` (niche, competitor, adjacent, far, provided; the closest is kept)
  and `found_by` (every phrase that found it, with its place in the results; `position` is null for provided
  videos, and YouTube entries also name the upload-date `window` the search used).
- A platform operation is skipped for the rest of the run when every route raised `NotConfigured` (the reason
  names each route's own message), or after 2 searches (5 other calls) in a row failed on every route; a
  lookup whose last error is `NotFound` (no such profile) does not count, and route memory does not count it as a route failure
  either. Skips and every route error go to `run_log.json`.
- The summary's counts give `accounts_over_cap`, `accounts_failed` (account histories asked for that came back from
  no route), `accounts_over_cap_above_floor` and `accounts_failed_above_floor` (those of them with a result at or
  above `outlier_views_min`, or with unknown views: only they could give a countable outlier, so only they make a
  second pass worth it), `paid_outliers`, `paid_unknown` (candidates whose `is_paid` no route reported: every
  YouTube one, Instagram Reels-tab posts, TikTok videos not marked as ads), `outliers_without_drop_flags`, and per
  platform how many candidates carry each flag and the `engagement_median` the boost check compared with.
- Per platform also: `accounts_with_candidates` (distinct accounts among the candidates: the ones whose history the
  scoring needs) and `accounts_with_posts` (account rows kept with at least one post, competitor accounts included,
  even those with no post inside `recent_days`), so the second can be larger than the first.
  `competitor_posts_undated` counts competitor posts with no known time (YouTube account histories carry no dates;
  an Instagram post is placed by its media id): they skip the `recent_days` check, and `competitor_posts_too_old`
  does not see them; only the details step dates the ones it looks up.
- Each scan starts with empty route memory (see Routes). The routes it stopped using are listed as `route_skips`
  in `run_log.json` and in the printed summary.
- YouTube search results carry no upload date, so YouTube is searched with the widest upload-date filter that
  reaches no further back than `recent_days` (60 days: this month). YouTube answers "No results found" for some
  popular phrases with a date filter, so an empty date-filtered Shorts search is widened up to a year, on both
  routes. `youtube.search` adds `window: {asked, used}` to its envelope; widened searches are listed as
  `widened_searches` in the summary and `run_log.json`. Those results have no date until the details step.

## Settings

`defaults/settings.json` holds every number with a `note` that says what it does.
A setting names its `type` (boolean, integer, number or text) when its default is null or is narrower than the
values it takes (the browser timeouts are numbers, so 30.5 fits); otherwise the default's own type is the type.
The user's file is checked against the defaults: a value of the wrong type stops every command with an error
naming the file; an unknown name is reported on stderr, by
`format-finder settings` and by doctor. `format-finder settings` hides `dtk_api_key` and any proxy password.
