# format-finder

A free, open-source Claude Code plugin. Give it an app brief; it finds outlier short-form videos on YouTube Shorts, TikTok and Instagram Reels, and turns them into ten formats worth testing.

An **outlier** is a video with far more views than its own account normally gets. The tool measures views only.

## What it does

1. Reads the brief and writes keyword search phrases at three distances: the app's niche, adjacent niches, and unrelated niches whose structures could carry over.
2. Finds competitor apps through Apple's free App Store search, and (with `apps --links`) the TikTok, Instagram and YouTube accounts each developer's own website links to.
3. Searches each platform for recent videos, pulls every account's recent posts, and works out each account's normal views (the median) and each video's multiple.
4. Flags videos that shouldn't be recommended: big accounts' ordinary videos, videos the platform marks as ads or paid partnerships (their posts are also left out of every account's normal; no YouTube route reports this, TikTok marks only ads, and the scan summary's `paid_unknown` counts videos whose paid status is not known), suspected boosts (views far out of line with likes), videos under a view floor (`outlier_views_min`), videos under `outlier_multiple_min` times the posts their account made just before them (`rising_account`: the account was already doing well; the video that started a rise is kept), accounts with too few posts to judge, accounts the scan never looked up (over the lookup cap), and accounts it could not look up (every route failed).
5. Downloads the strongest videos, cuts frames and gets captions or a local transcript, so Claude can describe each video's structure.
6. Groups videos into formats, then drops one-person hits (the format must work on several accounts, counting only real outliers, with accounts that share a caption phrase flagged as possible reposts of one show) and flags fading formats.
7. Writes ten formats, each with its evidence videos, their multiples, and how many accounts use it.

No hashtag search. No paid APIs. Everything runs on your own machine.

## Data routes

Each platform has two free routes; the tool switches to the next one when a route fails. When a route says the account or video does not exist (`NotFound`), the lookup ends there: the other routes are not asked. TikTok's route 1 is the exception: it says "not found" for a missing profile, a hidden one, or an answer with no profile data in it, so the other routes still ask. Within one scan, a route that failed twice in a row for an operation (or showed a captcha once) is skipped for the rest of the scan while another route remains; `run_log.json` lists these as `route_skips`.

| Platform | Route 1 | Route 2 | Notes |
|---|---|---|---|
| YouTube Shorts | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [YouTube.js](https://github.com/LuanRT/YouTube.js) | No login. |
| TikTok | [omkarcloud tiktok-scraper](https://github.com/omkarcloud/tiktok-scraper) | [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) (optional, you run it in Docker) | No login for route 1. Video search only answers from a US IP. yt-dlp covers account posts and videos as a fallback. Last: the **browser route** (search, account, video) opens TikTok's own pages in a headless browser; see [TikTok browser route](#tiktok-browser-route). |
| Instagram Reels | [instagrapi](https://github.com/subzeroid/instagrapi) | [aiograpi](https://github.com/subzeroid/aiograpi) | Both need a saved Instagram session (`instagram-login`). Last: the **browser route** (account, video, download; search only when you turn on `instagram_browser_search`) reads instagram.com's own pages in a headless browser you logged in to yourself; its search gives no views; see [Instagram browser route](#instagram-browser-route). |
| App Store | Apple iTunes Search API | none | No key. Google Play has no free route. |

Frames are cut with ffmpeg (installed through `imageio-ffmpeg`). Videos without captions are transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Requirements

- Claude Code
- Python 3.11 or newer, Node.js 22 or newer, git
- A Chromium-family browser for the TikTok and Instagram browser routes: Google Chrome, Chromium, Brave or Microsoft Edge. If none is installed, setup downloads Chrome for Testing (Google's free official build) into `~/.format-finder/browser` (382 MB unpacked on an Intel Mac in testing). The tool never uses your everyday browser profile. Tested on an Intel Mac with Brave and Chrome for Testing; installed Chrome, Chromium and Edge, Apple Silicon, Linux and Windows are untested.
- About 600 MB of disk after setup (a fresh setup took 567 MB on that Mac, without a browser download), plus the Whisper model, downloaded the first time a video has no captions
- Optional: Docker, only for TikTok route 2

## Install

In Claude Code:

```
/plugin marketplace add PaktoPak/format-finder
/plugin install format-finder@format-finder
/format-finder:setup
```

Setup installs everything into `~/.format-finder` (set `FORMAT_FINDER_HOME` to change it) and runs a health check.

**In your own terminal**, `format-finder` is not on your PATH: Claude Code puts the plugin's `bin/` folder on the PATH only for itself. Where this README says to run something in your own terminal, use the full path that `format-finder doctor` prints as `terminal_command` (quoted when it has spaces) in place of `format-finder`; below it is written `<terminal_command>`. The tool's own messages already give that full path.

### Instagram login (optional)

Every Instagram route needs a login, given in one of two ways:

- **A saved session** for the instagrapi and aiograpi routes (search, account history, videos, sounds, downloads): the steps below.
- **A login in the tool's own browser** for the browser route (account history, videos and downloads; search too if you set `instagram_browser_search` to true): run `<terminal_command> browser-login instagram` in your own terminal and log in yourself in the window it opens; see [Instagram browser route](#instagram-browser-route).

For a saved session:

1. Open instagram.com in your browser while logged in.
2. Copy the value of the `sessionid` cookie (Developer Tools → Application or Storage → Cookies).
3. In your own terminal, not in the Claude chat, run this and paste the cookie at the hidden prompt:
   ```bash
   <terminal_command> instagram-login --sessionid
   ```

The session is saved to `~/.format-finder/instagram_session.json`. The account you use can be restricted or banned by Instagram for automated use; some people use a spare account.

What is known about the session routes' replies: the parser was checked against 31 verified public samples (a 2026 capture of the Reels search reply's field paths, real account-reel and audio-page replies, media-info dumps). It found the right reels and cursors in all of them. Views are Instagram's `play_count`, which also counts Facebook plays when a reel is cross-posted there. Ads are left out of search results. Live requests through the instagrapi and aiograpi routes are **untested**: no session has been saved yet. Reels search pages after the first are also untested.

### Instagram browser route

The last Instagram route for account history and videos (and for search, when `instagram_browser_search` is true), after instagrapi and aiograpi. It opens instagram.com's own pages in the same headless browser and profile as the TikTok browser route (`~/.format-finder/browser-profile`) and reads what the page loads. It needs a login **you** made in that profile with `<terminal_command> browser-login instagram`; the tool never logs in, types nothing into the page, and never prints or stores a cookie, password or token. When a page ends on Instagram's login page, the route says (checked in unit tests; not seen live, since the profile was logged in; a logged-out page that shows no results without going to the login page would fail as a plain route error instead): *Instagram needs a login in the tool's browser: run <terminal_command> browser-login instagram in your own terminal and log in yourself (or use <terminal_command> instagram-login for the faster routes).*

What it reads (tested on 2026-11-05, one Mac, Brave, logged in):

- **Search** is **off by default** (`instagram_browser_search`: false): the keyword search page sends its own request for tag information about the keyword (`fetch__XDTTagInfo`; see below), and the tool asks for no hashtag or tag data. Without a saved session, Instagram search then does not run, and a scan reads Instagram only through competitor accounts and videos you give it. With the setting true, `format-finder search instagram "meal prep"` opens the keyword search page and keeps videos only, inside the date window. Ads are left out only when marked the way the app API marks them (`product_type` "ad" or an `ad_id`); web search items have no `product_type`, and no ad appeared in the recordings, so ad filtering on web search is untested. **Instagram's web search gives no views**: those results have likes, comments, date, duration and author, and `views` is empty. In a scan, each result's views come from its account's Reels tab (the same reel in the account history). A result that is not among the account's newest reels the scan reads (`baseline_posts`, 20) stays without views and is flagged `no_views`. Test: "meal prep", 60 days, 20 results in 27 s.
- **Account** opens the profile's Reels tab: follower count, and the newest reels with views, likes and comments, in grid order. The Reels tab gives **no dates and no captions**, so `posted_at` and `caption` are empty (a reel's own page, below, has both). For its `recent_days` window only, a scan places such a reel by the creation time its media id encodes (on the 12 reels of one scan it was 10 s to 7 min before the reel's posting time); `posted_at` stays empty. When the profile loads but its Reels-tab reply does not come or carries an error, the route fails with that error rather than returning an account with no reels (unit-tested only). Pinned reels are left out (the page's data marks them: a recording of one profile showed its first 3 reels marked), and so are reels another account posted (untested live). A profile with posts but no reels came back once live with its follower and post counts and no reels; `format-finder account` then says `empty: true`, with an `empty_reason`. Test: 20 reels and the follower count in 18 s.
- **Video** opens the reel's page: date, likes, comments, caption, author, sound and duration, no views. Test: 5 s; checked again after the reel's data began to be picked inside the page: the same fields in 5.5 s.
- **Download** (`format-finder media instagram <reel link>`) opens the reel's page, takes the smallest video file of at least 480 px width from the files the page lists (`video_versions`, MP4 with sound), and fetches it with a plain request that carries no cookie; the file's signed link is never printed or stored.
- **Sounds** have no browser route: they need the saved session.
- **A handle with no profile**: Instagram's "page isn't available" notice is recognised at the first quiet moment on the page (by its words in English, or in any language by its shape: no profile header, a link home, no page title tag), and the route answers `NotFound: Instagram has no profile @<name>` instead of scrolling an empty page. A bad handle is not counted as a route failure.
- A scan with Instagram only and browser search on (1 phrase, 12 results, 4 accounts) took 92 s; 3 of the 12 results got views from their account's Reels tab.
- Instagram sends every query of a page to one address (`/api/graphql`), so on search and profile pages the route receives every reply the page loads there and keeps only the search, Reels-tab and profile data. One of those replies carried an encrypted value (`get_encrypted_credentials`); the route receives it in memory with the rest and ignores it, and never prints or stores it. On a reel page the route reads no replies, only the reel's data from the page's embedded JSON, picked inside the page: the page's config script, which holds its csrf and DTSG tokens, is not read. The keyword search page makes its own request for tag information about the keyword (`fetch__XDTTagInfo`, answered null for "meal prep" in a recording); the route does not ask for it and ignores the reply, and this request is why browser search is off by default.

Untested: a private profile, a "page isn't available" notice in a language other than English or French, collab reels, ads in web search results, how close a media id's time is to the posting time for a reel saved as a draft or scheduled, what happens when the login in the tool's browser expires, and how many pages Instagram serves before it limits the account.

**Account risk:** the browser route acts through the Instagram account you logged in with, and Instagram can restrict or ban an account for automated use; some people use a spare account. To log out, delete `~/.format-finder/browser-profile` (that also removes a TikTok login there). `"browser_route": false` turns the TikTok and Instagram browser routes off.

### TikTok outside the US

TikTok answers video search only from a US IP. From elsewhere, search fails, while account history, videos and sounds still work. Set a proxy URL as `tiktok_proxy` in `~/.format-finder/settings.json` if you have one.

### TikTok browser route

The last TikTok route for search, account history and videos. It opens TikTok's own pages in a headless browser with a profile of its own (`~/.format-finder/browser-profile`) and reads what the page loads. It types nothing into the page and never reads, stores or prints a cookie, password or token. Turn it off with `"browser_route": false` in the settings file.

**The captcha limit, plainly:** TikTok flags an IP after repeated logged-out searches. In testing, the first headless search from a fresh profile got full results; after repeated test searches, every search from that IP got a slider puzzle or an empty reply, with any browser or profile. Profile pages then showed the same puzzle when their post list loaded. Video pages still read normally. How long the flag lasts is not known. The tool never solves or bypasses a puzzle: after the first one, a scan stops using the browser route for that operation and carries on with what works. `format-finder doctor --live` says whether search through the browser works right now.

**Logging in (optional).** `<terminal_command> browser-login tiktok`, run in your own terminal, opens a visible window of that same profile at TikTok's login page. You log in yourself, or complete the puzzle yourself, then close the window; the tool types nothing into it. Whether a login or a solved puzzle stops the puzzles for the headless route is **untested**. If you log in, the browser route then acts through your TikTok account, and TikTok can restrict or ban an account for automated use; some people use a spare account.

### TikTok route 2 (optional)

Run [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) yourself with Docker, following its quick start. Keyword search through it needs a logged-in TikTok account's cookies imported in its console. Then set `dtk_url` (for example `http://127.0.0.1:8000`) and `dtk_api_key` in `~/.format-finder/settings.json`.

## Use

```
/format-finder:find-formats <your app brief>
```

Each run writes to `format-finder-runs/<timestamp>/` in the current folder: `terms.json`, `account_checks/` (the competitor accounts checked by hand), `candidates.json`, `accounts.json`, `run_log.json`, `media/`, `groups.json` and `formats.md`. The scan reuses the saved account checks (`--accounts-from <run>/account_checks`), so it does not ask for those accounts again. A follow-up scan goes in its own folder (`<run>/followup-1/`) and can reuse the account histories already fetched with `--accounts-from <run>` (add `--accounts-from <run>/more-accounts` after a second pass).

You can also call the command-line tool directly; every command prints JSON (in your own terminal, use `<terminal_command>` in place of `format-finder`):

```bash
format-finder search youtube "meal prep for night shift" --days 30
format-finder account tiktok HANDLE
format-finder video youtube "https://www.youtube.com/shorts/VIDEO_ID"
format-finder sound tiktok MUSIC_ID
format-finder apps "meal planner" --links
format-finder media youtube "https://www.youtube.com/shorts/VIDEO_ID" --out ./media-test
format-finder settings
format-finder doctor --live
format-finder browser-login tiktok
format-finder browser-login instagram
```

## Settings

`format-finder settings` lists every setting you can change, each with a note on what it does; the table below lists the main ones. The numbers are rules of thumb, not audited studies, which is why you can change them. A few fixed limits stay in the code, such as the 2 failed calls in a row after which a scan stops using a route. Override any of them in `~/.format-finder/settings.json`, for example:

```json
{ "recent_days": 30, "outlier_multiple_min": 10 }
```

| Setting | Default | What it does |
|---|---|---|
| `recent_days` | 60 | Only videos from roughly the last month or two |
| `baseline_posts` | 20 | Posts read from each account to work out its normal views |
| `outlier_multiple_min` | 5 | Times its account's normal views a video needs to be an outlier (10 keeps only the strongest) |
| `top_fraction` | 0.1 | Top ~10% of a batch; the scan flags them, and the skill looks at those videos first |
| `min_accounts_per_format` | 5 | Separate accounts a format needs, so one creator's hit is not taken for a format |
| `outlier_views_min` | 5000 | View floor: videos under it are flagged `low_views`, whatever their multiple |
| `boost_ratio` | 0.1 | Flags videos with far fewer likes and comments per view than the batch (suspected boost) |
| `baseline_min_posts` | 5 | Fewest unpaid posts with views an account needs for a normal (else `too_few_posts`) |
| `recent_normal_posts` | 6 | Posts made just before a video that make its recent normal (`rising_account`) |
| `fading_min_span_days` | 14 | Days the dated videos must span before check-formats compares older and newer halves |
| `in_niche_count` | none | How many of the ten must come from the app's own niche; none means no fixed split |
| `browser_route` | true | false removes the TikTok and Instagram browser routes |
| `browser_path` | none | The browser program to use; none finds one |
| `instagram_browser_search` | false | true lets Instagram search use the browser route, whose keyword search page sends its own tag-information request |

## Limits and risks

- **Platform terms.** YouTube's terms ban automated access and downloading without permission. TikTok's US terms (§3.4) ban scraping without written approval. Instagram's terms (§4.2) ban automated collection, logged in or not. You are responsible for how you use this tool.
- **Accounts.** The Instagram routes act through your logged-in session or the account you logged into with `format-finder browser-login instagram`, and that account can be restricted or banned. The same goes for a TikTok account you log into with `format-finder browser-login tiktok`.
- **Rate limits.** Requests come from your own IP. YouTube bot checks, TikTok blocks and puzzles, and Instagram limits can stop a route; the tool records this in `run_log.json` and carries on with the routes that work. TikTok search through the browser route can stop working from your IP after a few searches (see [TikTok browser route](#tiktok-browser-route)).
- **Breakage.** Every route depends on unofficial endpoints that change. Re-run `/format-finder:setup` to update.
- **Coverage.** No free route lists trending sounds on YouTube or Instagram. Google Play is not covered.

## Licence

MIT. The routes it installs keep their own licences: yt-dlp (Unlicense), YouTube.js (MIT), omkarcloud tiktok-scraper (MIT), Douyin_TikTok_Download_API (Apache-2.0), instagrapi (MIT), aiograpi (MIT), faster-whisper (MIT), imageio-ffmpeg (BSD-2-Clause; the ffmpeg binary it ships is under ffmpeg's own licence), websockets (BSD-3-Clause), @puppeteer/browsers (Apache-2.0; run only when setup downloads Chrome for Testing).
