---
name: setup
description: Install and check everything format-finder needs (Python packages, YouTube.js, the TikTok scraper, a browser for the TikTok and Instagram browser routes), and explain the optional Instagram login (two ways), TikTok browser login, TikTok proxy and TikTok route 2.
disable-model-invocation: true
allowed-tools: Bash(bash *setup.sh*), Bash(format-finder doctor*), Bash(format-finder settings*)
---

# format-finder setup

Run the setup script. It installs into `~/.format-finder` (or `$FORMAT_FINDER_HOME`), is safe to re-run, and ends with a health check.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh"
```

If it stops with `ERROR:`, tell the user the exact line and the fix it names (Python 3.11+, Node 22+ or git missing). Do not try to install system programs yourself.

The "Browser for the TikTok browser route" step (the Instagram browser route uses the same browser) uses an installed Chrome, Chromium, Brave or Edge. Only when none is found does it download Chrome for Testing (Google's free official build, a few hundred MB) into `~/.format-finder/browser`. If it prints `WARNING:`, tell the user: the other routes still work, and re-running setup tries the download again.

When it finishes, run a live check and show the user which routes work from their machine:

```bash
format-finder doctor --live
```

In the live check's `platforms.tiktok`, `search_via_browser` and the notes say plainly whether TikTok search works through the browser right now. If a note says TikTok is asking this IP for a puzzle, tell the user exactly that: TikTok flags an IP after repeated logged-out searches, and how long that lasts is not known.

Then explain the optional pieces, in plain words, only as far as they apply. Some of them are commands the user runs **in their own terminal**. There, `format-finder` is not on the PATH (the plugin's command is on the PATH only inside Claude Code), so give them the live check's `terminal_command` in its place: the tool's full path, quoted when it has spaces. Below, `<terminal_command>` stands for that value.

1. **Instagram** needs a login, and there are two ways to give it. Either works on its own; both can be set up.
   **a. A saved session (`instagram-login`)** for the instagrapi and aiograpi routes: search, account history, videos, sounds and downloads. Say plainly that these routes are untested live: no session has been saved yet, so whether Instagram answers them is not known.
   - The user copies the `sessionid` cookie from instagram.com in their own browser (Developer Tools, Application or Storage tab, Cookies, `sessionid`).
   - They run this **in their own terminal**, not in this chat, and paste the cookie at its hidden prompt (so it stays out of shell history and the process list):
     `<terminal_command> instagram-login --sessionid`
   - Never ask the user to paste the cookie into the chat, and never type it into a command yourself.
   - Tell them plainly: the account used can be restricted or banned by Instagram for automated use. Some people use a spare account.
   **b. A login in the tool's own browser (`browser-login instagram`)** for the browser route: account history, videos and video downloads, read from instagram.com's own pages (search too, only if the user sets `instagram_browser_search` to true). The user runs this **in their own terminal**:
   `<terminal_command> browser-login instagram`
   It opens a visible window of format-finder's own browser profile (not their everyday browser) at Instagram's login page. **They** log in themselves, then close the window. The tool types nothing into it and never reads a password, cookie or token.
   - Never ask for their username, password, code or cookie, and never type anything into that window.
   - Say plainly what the browser route cannot do: it does not search unless `instagram_browser_search` is true (off by default, because instagram.com's keyword search page sends its own tag-information request for the keyword); with it on, Instagram's web search gives no views (a scan takes each result's views from its account's Reels tab, so results not among an account's newest reels stay without views), the Reels tab gives no dates, and sounds need the saved session from way a.
   - Doctor cannot tell offline whether that browser is logged in: it shows the Instagram browser route as `null` (unknown). When it is not, the browser route fails with `NotConfigured: Instagram needs a login in the tool's browser ...` (checked in unit tests; not yet seen live).
   - Tell them plainly: the browser route acts through the Instagram account they logged in with, and Instagram can restrict or ban an account for automated use. Some people use a spare account.
2. **TikTok search outside the US.** TikTok answers video search only from a US IP. Outside the US, search fails while account history, videos and sounds still work. A proxy URL can be set as `tiktok_proxy` in the settings file (`format-finder settings` prints its path as `user_file`).
3. **TikTok route 2** (Douyin_TikTok_Download_API) is optional and runs in Docker, which the user installs and starts themselves. Keyword search through it needs a logged-in TikTok account's cookies imported in its console. Once it runs, set `dtk_url` and `dtk_api_key` in that settings file.
4. **TikTok browser login** (optional, only if TikTok shows the browser route a puzzle or a login wall). The user runs this **in their own terminal**:
   `<terminal_command> browser-login tiktok`
   It opens a visible window of format-finder's own browser profile (not their everyday browser) at TikTok's login page and waits until they close it. **They** log in, or complete the puzzle, themselves in that window. The tool types nothing into it and never reads a password, cookie or token.
   - Never ask for their username, password, code or cookie, never type anything into that window, and never try to solve, bypass or automate a puzzle.
   - Say plainly that whether logging in or completing the puzzle stops the puzzles for the headless route is untested.
   - Tell them plainly: if they log in, the browser route acts through that TikTok account, and TikTok can restrict or ban an account for automated use. Some people use a spare account.
   - To turn the browser route off entirely (TikTok and Instagram): `"browser_route": false` in the settings file.

To show the settings the tool uses, each with a note on what it does: `format-finder settings`.
