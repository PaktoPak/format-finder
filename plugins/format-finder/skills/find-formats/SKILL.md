---
name: find-formats
description: Turn an app brief into ten short-form video formats worth testing, backed by outlier videos found live on YouTube Shorts, TikTok and Instagram Reels.
when_to_use: The user gives an app brief (or names an app) and wants short-form video formats, viral formats, outlier videos or reference videos for it.
allowed-tools: Bash(format-finder *), Read, Write
---

# Find ten formats for an app

Input: the app brief in the user's message (or `$ARGUMENTS`). Output: ten formats, each backed by real outlier videos.

Ground rules for the whole run:
- **Views are the measure.** Do not discuss signups, downloads or conversion.
- **No hashtags.** Never search by hashtag and never suggest it. Search phrases are plain keywords.
- **Never invent numbers.** Every number you report comes from the tool's JSON output or from `format-finder settings`.
- **Never handle credentials.** If Instagram is not logged in, tell the user to run `/format-finder:setup`; never ask for a cookie or password in the chat.
- **Commands for the user's own terminal** (`instagram-login`, `browser-login`): `format-finder` is on the PATH only inside Claude Code, so give the user doctor's `terminal_command` (the full path, quoted when it has spaces) in its place, written `<terminal_command>` below. The tool's own messages already name that full path.
- Every command prints JSON. Read it; when `ok` is false, report the `errors` and carry on with what works.

Make a run folder first: `format-finder-runs/<YYYYMMDD-HHMMSS>/` in the current directory, with an `account_checks/` folder inside it. Everything for this run goes there.

## 0. Check the tool

```bash
format-finder doctor
```
If it says the tool is not set up, stop and tell the user to run `/format-finder:setup`. Note which platforms are available (Instagram needs a login: a saved session, or a login the user made in the tool's browser; TikTok search needs a US IP, route 2, or the browser route while TikTok still answers it from this IP). Instagram `available: null` means only its browser route could run and doctor cannot tell whether the user logged in there: keep Instagram in the scan. When an Instagram page ends on Instagram's login page, the scan records `NotConfigured: Instagram needs a login in the tool's browser` and skips that Instagram operation (checked in unit tests; not yet seen live); a logged-out page that shows no results without going to the login page fails as a plain route error instead. A timeout is not a login sign (`did not load within`, `gave no results within`, `did not answer`): retry that command once. Tell the user to run `<terminal_command> browser-login instagram` in their own terminal and log in themselves only on that `NotConfigured` login message, on a `BotCheck:` that names a login page or login wall, or when a page `loaded but carried no results` twice. `NotFound:` as the last error is not a login problem: Instagram or TikTok says that handle has no profile, so drop the handle; the tool asks no later route after it. TikTok's route 1 cannot tell a missing profile from a hidden one or from an answer with no profile data in it, so after its `NotFound` the later routes still ask; when they fail too (a captcha, say), the handle is unconfirmed, not missing. Without a saved session, Instagram **search** does not run: its browser route is off unless the user sets `instagram_browser_search` to true, because instagram.com's keyword search page sends its own tag-information request (`fetch__XDTTagInfo`); the scan's errors say so. Instagram then comes only from competitor accounts and videos the user gives, so ask for Instagram accounts they know. Tell the user up front which platforms this run will cover.

## 1. Read the brief

Pull out, in the user's own words where possible:
- what the app does, who it is for, the problem it solves, its category
- **problem words** (how the audience describes the pain), **audience words** (who they are, their communities), **category words**

If the brief is too thin to write search phrases (no audience or no problem), ask one short question, then continue.

## 2. Competitors and search phrases

Find competitor and adjacent apps:
```bash
format-finder apps "<category phrase>" --country us --limit 25 --links
```
Use the results to name competitors and adjacent apps. (Google Play has no free route; say so if Android matters.) With `--links`, each app's `social_links` lists the TikTok, Instagram and YouTube profiles its developer's own website (`developer_url`) links to: take competitor handles from there first. Check every handle before you add it, and save the answer in the run folder:
```bash
format-finder account <platform> <handle> > "<run>/account_checks/<platform>-<handle>.json"
```
(For a YouTube link such as `youtube.com/c/Name`, pass it as given and use only `Name` in the file name.) Drop a handle when the account's name, followers and posts do not look like the brand (a guessed handle is often someone else's). `empty: true` means no posts came back, so the account gives nothing to score (`empty_reason` says why). With no followers either, a missing, private or blocked account looks the same. With followers or a post count, the profile answered and listed no posts, for example an Instagram profile with no reels. `NotFound:` as the last error means no such profile. The scan reuses these saved histories (`--accounts-from`, below), so the accounts you checked are not asked for again (one that came back with nothing at all is asked again).

Write search phrases at three distances:
- **niche:** the app's problem, audience and category (6-10 phrases)
- **adjacent:** the same audience with other interests, or the same problem for other audiences (4-6)
- **far:** unrelated niches whose video structures could carry over (3-5)

Competitor accounts: if the user named any TikTok, Instagram or YouTube accounts (their own or competitors'), include them. Instagram accounts cannot be discovered by the tool other than through search and the developer websites `apps --links` reads, so ask the user for any others they know.

Save `terms.json` in the run folder:
```json
{
  "niche": ["..."],
  "adjacent": ["..."],
  "far": ["..."],
  "competitor_accounts": {"youtube": [], "tiktok": [], "instagram": []},
  "videos": [{"platform": "tiktok", "ref": "https://..."}]
}
```
`videos` holds any links the user supplied themselves.

## 3-5. Collect videos, account histories and outliers

```bash
format-finder scan --terms "<run>/terms.json" --platforms youtube,tiktok,instagram --out "<run>" --accounts-from "<run>/account_checks"
```
Drop platforms doctor said are unavailable (`available: false`). Leave out `--accounts-from` if you saved no account checks. The scan searches every phrase, pulls each account's recent posts, works out each account's normal views (median) and each video's multiple (views ÷ normal), and writes:
- `<run>/candidates.json`: every video, scored and sorted by multiple
- `<run>/accounts.json`: account histories
- `<run>/run_log.json`: which routes worked, which failed, what was skipped

Read the printed summary, then `candidates.json`. Tell the user which routes were used and anything that failed or was skipped.
- `counts.accounts_over_cap` and `counts.accounts_failed`: candidates whose account history was never asked for (over the cap: flag `account_not_looked_up`) or came back from no route (every route failed, or the scan had stopped asking after failures: flag `account_lookup_failed`) cannot be scored. The cap goes first to accounts with a result that can pass `outlier_views_min`. When those counts are large next to `account_lookups`, tell the user how many accounts went unscored and why (the failed ones' errors are in `errors` and `skipped`). `accounts_over_cap_above_floor` and `accounts_failed_above_floor` (also per platform in `per_platform`) count the unscored accounts with a result at or above `outlier_views_min` (as the search listed its views) or with unknown views. Only those could give a countable outlier; the others' results would all be flagged `low_views`. Offer a second pass only when one of these two counts is above 0, and only on the platforms where it is: `format-finder scan --terms "<run>/terms.json" --platforms <those platforms> --out "<run>/more-accounts" --max-accounts <N> --accounts-from "<run>"`. Leave TikTok out when `route_skips` shows a captcha for `tiktok.search`: a new scan starts without route memory and would ask TikTok to search again. When both counts are 0 on every platform, offer no second pass, and say the unscored accounts' results were all under the view floor. The second pass reuses the histories already fetched and changes no settings file, but it repeats every search, repeats the detail lookups (up to `max_details_per_run`), asks again for the accounts that failed (they may fail again), and costs one more request from the user's IP per extra account (this run's `duration_s` in `run_log.json` shows how long it took). It is the user's choice; if they decline, say in `formats.md` that the number of formats may be limited by the cap or by the failed lookups.
- `counts.paid` and `counts.paid_outliers`: videos, and outliers, the platform marks as paid (flag `paid`: an ad, or on Instagram a paid partnership). Say how many there were, and name any account most of whose posts are paid (`account_paid_posts`): it has no organic normal. `paid_unknown` (per platform in `per_platform`) counts candidates whose paid status no route reported. No YouTube route reports it. Instagram's Reels tab does not either; only its reel pages (the detail step) and the session routes do. TikTok marks ads but never a paid partnership. So when a platform's `paid_unknown` equals its `candidates`, write "paid status not reported" for it, not "0 paid videos" (for TikTok: "none marked as an ad; paid partnerships are not reported").
- `route_skips` lists routes the scan stopped using for one operation, for example `{"op": "tiktok.search", "route": "browser", "captcha": true}`. `captcha: true` means TikTok asked this IP for a puzzle. Say so plainly: TikTok flags an IP after repeated logged-out searches; the user can try later, or run `<terminal_command> browser-login tiktok` in their own terminal and log in or complete the puzzle themselves in the window it opens (whether that helps is untested). Never try to solve or get around a puzzle, and never ask for a login.
- Instagram through the browser route (`routes_used` shows `browser` for instagram): its search results carry no views (instagram.com's search gives none). Their views come from each account's Reels tab in the account step, so a result whose account was not looked up (over `max_accounts_per_run`) or that is not among the account's newest reels stays `no_views`. Reels-tab posts have no `posted_at`; the scan keeps them inside `recent_days` by the time their media id encodes (within minutes of the posting time in testing), and `posted_at` stays empty. Say so when you report Instagram numbers.
- In `errors`, `BotCheck:` marks a captcha, puzzle, login wall or empty reply from the browser route, and `RouteSkipped:` a route that route memory skipped. YouTube search results carry no upload date: YouTube is searched with the widest date filter inside `recent_days` (60 days: "this month"), and `widened_searches` lists the phrases that found nothing there and were widened (up to "this year"). Each YouTube `found_by` entry names its `window`.

## 6. Remove what should not be recommended

Work from `candidates.json`. Each candidate has `multiple`, `account_normal`, `recent_normal`, `views`, `engagement_rate`, `boost_checked`, `batch_rank` and `flags`.
- Drop `no_views`, `too_few_posts` (the history came back with too few posts for a fair normal), `account_not_looked_up` and `account_lookup_failed` (no history, so never scored; see the note on `accounts_over_cap` and `accounts_failed` above).
- Keep only `meets_multiple` (at or above the `outlier_multiple_min` setting). This also drops big accounts' ordinary videos, because their multiple is low.
- Drop `low_views`: fewer views than the `outlier_views_min` setting, however high the multiple. Views are the measure; a few dozen views on a normal of 2 is noise.
- Drop `paid`: the platform marks the video as an ad (or on Instagram a paid partnership), so its views may be bought. Paid posts are also left out of every account normal (`account_paid_posts` says how many). If a paid video is the only evidence for something, mention it only with that label, never as organic proof.
- Drop `rising_account`: the video beats the account's normal but not the posts the account made just before it (`recent_normal`, the median of the `recent_normal_posts` posts before it), so the account had already taken off and the video was ordinary for its output at the time. A video that started the rise is not flagged. `recent_normal` is null when fewer than `recent_normal_posts` posts came before it in the account history (an older video): then the check could not run.
- Drop `suspected_boost` (views far out of line with likes and comments). `boost_checked: false` means this check could not run (no like count): say so for any such video you use.
- A YouTube candidate with no `posted_at` may be old when no search found it inside `recent_days`: its `found_by` has only windows wider than `recent_days` (for example `year`), or no window at all (a competitor post: YouTube account histories carry no dates, so these posts skip the date check; `competitor_posts_undated` counts them, and `competitor_posts_too_old` does not see them). The scan dates only the outliers its detail step looks up (up to `max_details_per_run`). Check any such candidate you use that is still undated with `format-finder video youtube "<url>"`, and drop it if it is older than `recent_days`.
- **Riding a moment** (a celebrity, news story or trending sound) is allowed, but only counts later if the same pattern works with different moments on different accounts. Flag any video that uses a real person's voice or likeness as a rights risk.
- **One-person hits** and **burned-out formats** are checked in step 7, after grouping.

## 7. Look at the videos and group them into formats

Pick the strongest remaining candidates, about 30-40 across platforms, or all of them when fewer remain. Never fill up with videos that carry a drop flag. Videos from follow-up scans join the same list. Start with those flagged `top_fraction` (the top share of the scan's scored videos by multiple, set by the `top_fraction` setting), then fill up by raw views as well as multiple (a huge multiple on a normal of a few views is weaker evidence than a smaller multiple on a normal of thousands). For each:
```bash
format-finder media <platform> "<url>" --out "<run>/media/<platform>-<id>"
```
This downloads the video, cuts frames weighted toward the opening, and returns captions or a local Whisper transcript. **Read the frame images** listed in the output and the transcript. Instagram downloads use the saved session (`instagram-login`; its routes are untested live, since no session has been saved yet) or, without it, the browser route (the reel's own page lists its video file); if both fail, say so and leave that video out of this step rather than guessing its structure.
- A Whisper transcript with `speech: false` has no speech; `low_confidence: true` means the text may be made up (music-only videos often come back as a short repeated line): rely on the frames.
- `language_mismatch: true` means the captions' language (`transcript_language`) differs from the language Whisper hears (`audio_language`): the caption track may be the wrong one; say so and rely on the frames and on-screen text. `captions_language_checked: true` means the captions' language matched the audio language YouTube reports. `false` with no `audio_language` means it was never checked (Whisper off or failed); say so when the transcript matters.

For each video, write down its structure with the topic removed:
- the opening (first frame, first line, first tension)
- the sequence of beats, with rough seconds
- length, on-screen elements (text, screen recording, face, props) and sound use
- separately: the **angle** (the promise or framing) and the **hook** (the opening seconds)

Group videos with the same structure into one format, even if their topics differ. A video belongs to a format only when it has that format's defining opening and beat sequence: one that contradicts a beat (one scenario where the beat sheet needs several, people on camera where it says a static frame) does not belong, and neither does one that only shares a production style (animation, AI footage, kinetic text is a style, not a structure). Never widen a format's description to take in more videos. Save the grouping as `<run>/groups.json`, mapping each format name to the list of its candidate records, each copied from the `candidates.json` of the scan that scored it (its `multiple` is not null; records may come from several scans). When two scans give one video different flags, use the record with the drop flag, or call the video borderline and do not count it: YouTube history views are rounded, so a multiple near a line can flip between scans. Then run:
```bash
format-finder check-formats --groups "<run>/groups.json"
```
It reports, per format: `accounts` (distinct accounts among the videos that count: real outliers without a drop flag from step 6), `not_counted` (the others and why), whether `accounts` meets `min_accounts_per_format`, `accounts_after_shared_captions` and `warnings`, the median multiple (per video and per account), the median views, and older-half vs newer-half medians.
- **Shared captions:** `shared_captions` has one entry per set of accounts whose captions share a run of words: its `accounts`, the `phrases` they share, and `accounts_if_one_producer` (the format's account count if that set alone is one producer). Judge each entry on its own phrases. If they name one show, series or creator (reposts of one programme), count that set as one producer; if they are the format itself or a common line such as a call to action, they are separate producers. Say which you decided for each. `accounts_after_shared_captions` is the lowest count, with every entry taken as one producer; when you accept only some entries, count from those (accounts in two accepted entries join both sets). An Instagram video that had no detail lookup has no caption (the Reels tab gives none), so this check cannot judge it: say so. A name posting on two platforms is probably one creator. So are two videos on two platforms, under different names, with the same transcript or the same length (within about 1 s): count them as one producer. check-formats warns (`possibly one video`) when two counted videos on two platforms are within 1 s in length and, when both dates are known, within 7 days; compare the two videos before you decide.
- **One-person hit:** fewer distinct producers than `min_accounts_per_format`. Do not recommend it. If more examples would settle it, run a follow-up scan aimed at that structure: write `<run>/followup-<N>/terms.json` and run `format-finder scan --terms "<run>/followup-<N>/terms.json" --platforms <platforms> --out "<run>/followup-<N>" --accounts-from "<run>"` (add `--accounts-from "<run>/more-accounts"` when a second pass ran, and `--accounts-from` for each earlier follow-up folder). Never use `--out "<run>"` (it would overwrite the main scan) and do not run a separate `format-finder search` first: scan searches and scores in one go. A phrase aimed at a structure keeps the distance of the topic words in it: "red flag or green flag money" is niche only through "money", so a video it finds about dating is not in-niche. Skip TikTok for this when `route_skips` shows a captcha for `tiktok.search`: each new command starts without the scan's route memory, so it would ask TikTok again and get the puzzle again. YouTube search records name the channel only by id (`author_id`) until the scan has looked the account up.
- **One main producer:** when one account supplies most of a format's videos, list that account's other posts in the same series from `accounts.json` with their views, so the reader sees its hit rate, not only its hits. Instagram Reels-tab posts carry no caption and no date, so the series cannot be told by title there: list that account's reel views from `accounts.json` and say the series could not be told apart, or look up its newest reels with `format-finder video instagram "<url>"` first.
- **Burned out:** the newer half's median multiple is clearly below the older half's. There is no exact rule here, so state both numbers and say what you decided. `fading.too_recent: true` means the dated videos span less than `fading_min_span_days` (`span_days` says how long): the burnout check cannot judge that format; say so. The halves are compared only when `dated_videos` reaches `min_dated_videos` (4); with fewer, say the check had too few dated videos.

## 8. Check fit with the app

For each surviving format: can the app take the place of the original subject inside the structure? Formats that rely on insider humour of another community carry over poorly. Mark each format **in-niche** or **borrowed** by what its counted videos are about and how many distinct producers made the in-niche ones, not only by the `distance` label of the phrase that found them (niche or competitor suggests in-niche; adjacent or far suggests borrowed). A `provided` distance means the video came from the user's own links; say so next to it.

## 9. Write the ten

Rank by median multiple, median views, number of distinct producers, how recent the videos are, and fit. No two picks may share a format. If `in_niche_count` is set in `format-finder settings`, use that split; otherwise rank on evidence alone. If fewer than ten formats survive the checks, give fewer and say why. Do not pad: a format that reaches `min_accounts_per_format` only by counting loose fits, reposts of one show, or `not_counted` videos goes under "did not survive".

For each format write:
- **Name** and one-line description of the structure
- **Beat sheet** (opening, beats with rough seconds, ending), written from what the counted videos show. If they split into variants (a different setting, cast or prompt), describe each variant with its own videos' numbers.
- **How the app fits in** (what replaces the original subject)
- **Evidence:** the outlier videos (link, platform, account, views, account normal, multiple), the number of distinct producers, the median views, older vs newer medians. Note any video whose `engagement_rate` is far below the other videos of its account or format, or only just above the `suspected_boost` line (`boost_ratio` x that platform's `engagement_median` in the scan summary), and any video that `boost_checked: false` left unchecked.
- **In-niche or borrowed**
- **Risks:** rights (real person's voice or likeness, borrowed audio), fading signal

Save it as `<run>/formats.md` and show it to the user. End with one short line on what this run could not cover (platforms skipped, routes that failed).

See `reference.md` in this folder for the reason behind each rule.
