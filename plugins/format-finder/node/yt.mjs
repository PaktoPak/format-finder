// format-finder, YouTube route 2: YouTube.js (InnerTube, no login, no API key).
// Python runs `node yt.mjs <command> ...` and reads one JSON document from stdout:
// {"ok": true, "result": ...} or {"ok": false, "error": "..."}. Logs go to stderr.
//
//   search <query> [--upload-date today|week|month|year] [--limit N] [--region XX] [--delay S]
//   channel <@handle|UC id|channel URL> [--limit N] [--delay S]
//   video <id|url>
//   sound <id|url> [--limit N] [--delay S]
//   download <id|url> --out DIR [--ffmpeg PATH]
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

// Keep stdout for the JSON answer only.
console.log = console.info = console.debug = console.warn = (...args) => console.error(...args);
const { Innertube, Log, Platform } = await import('youtubei.js');
Log.setLevel(Log.Level.ERROR);

const log = (msg) => process.stderr.write(`${msg}\n`);
const sleep = (s) => new Promise((r) => setTimeout(r, Math.max(0, s) * 1000));
const int = (v, d) => (Number.isFinite(parseInt(v, 10)) ? parseInt(v, 10) : d);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const maxPages = (limit) => Math.max(1, Math.min(5, Math.floor((limit || 30) / 20) + 2));

// A later page failing keeps the pages already read.
async function nextPage(fn, have) {
  try {
    return await fn();
  } catch (e) {
    log(`continuation failed, keeping ${have} results: ${e.message}`);
    return null;
  }
}
const shortsUrl = (id) => `https://www.youtube.com/shorts/${id}`;
const channelLink = (id) => (id ? `https://www.youtube.com/channel/${id}` : null);
const stripAt = (h) => (h ? String(h).replace(/^@/, '') : null);

function client(region) {
  const opts = { lang: 'en', retrieve_player: false };
  if (region) opts.location = String(region).toUpperCase();
  return Innertube.create(opts);
}

function parseArgs(argv) {
  const pos = [];
  const opt = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) opt[argv[i].slice(2)] = argv[++i];
    else pos.push(argv[i]);
  }
  return { pos, opt };
}

// '1.7M views' -> 1700000, '423K subscribers' -> 423000. YouTube rounds counts in lists.
function count(text) {
  if (text == null) return null;
  if (typeof text === 'number') return Number.isFinite(text) ? Math.round(text) : null;
  const s = String(text);
  if (/^\s*no\b/i.test(s)) return 0;
  const m = s.match(/(\d[\d,]*(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)?(?![a-z])/i);
  if (!m) return null;
  const mult = { '': 1, k: 1e3, t: 1e3, m: 1e6, b: 1e9 }[(m[2] || '').toLowerCase().slice(0, 1)];
  return Math.round(parseFloat(m[1].replace(/,/g, '')) * mult);
}

function videoId(ref) {
  const r = String(ref).trim();
  if (/^[\w-]{11}$/.test(r)) return r;
  const m = r.match(/(?:[?&]v=|\/shorts\/|youtu\.be\/|\/embed\/|\/live\/|\/source\/)([\w-]{11})/);
  if (!m) throw new Error(`not a YouTube video id or URL: ${r}`);
  return m[1];
}

function handleFromUrl(url) {
  const m = String(url || '').match(/\/@([^/?#]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function isoOrNull(value) {
  const t = value ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? new Date(t).toISOString().replace('.000Z', 'Z') : null;
}

// Shorts lockups have no channel field; the uploader's UC id sits in reelWatchEndpoint.params (proto field 23).
function channelFromParams(params) {
  if (!params) return null;
  const raw = Buffer.from(decodeURIComponent(params), 'base64').toString('latin1');
  const m = raw.match(/\xba\x01\x18(UC[\w-]{22})/) || raw.match(/(UC[\w-]{22})/);
  return m ? m[1] : null;
}

function short(id, fields) {
  const record = { id, url: shortsUrl(id) };
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) record[k] = v;
  return record;
}

function withExtra(fields, extra) {
  for (const [k, v] of Object.entries(extra || {})) if (v != null) fields[k] = v;
  return fields;
}

// Parsed YouTube.js nodes (search results, channel Shorts tab).
function nodesOf(root, types, out = [], depth = 0) {
  if (!root || depth > 8 || typeof root !== 'object') return out;
  if (Array.isArray(root)) {
    for (const n of root) nodesOf(n, types, out, depth);
    return out;
  }
  if (types.includes(root.type)) {
    out.push(root);
    return out;
  }
  for (const key of ['contents', 'items', 'content']) if (root[key]) nodesOf(root[key], types, out, depth + 1);
  return out;
}

function fromNode(n, extra) {
  if (n.type === 'ShortsLockupView') {
    const p = n.on_tap_endpoint?.payload || {};
    const id = p.videoId || String(n.entity_id || '').replace(/^shorts-shelf-item-/, '');
    if (!/^[\w-]{11}$/.test(id)) return null;
    const a11y = n.accessibility_text || '';
    const caption = n.overlay_metadata?.primary_text?.text ?? a11y.match(/^(.+), [^,]+ views? - play Short$/)?.[1] ?? null;
    let views = count(n.overlay_metadata?.secondary_text?.text);
    if (views == null) views = count(a11y.match(/, ([^,]+ views?) - play Short$/)?.[1]);
    const cid = channelFromParams(p.params);
    return short(id, withExtra({ caption, views, author_id: cid, author_url: channelLink(cid) }, extra));
  }
  // Classic Video / ReelItem nodes: keep only Shorts.
  const id = n.video_id || n.id;
  const url = n.endpoint?.metadata?.url || '';
  const duration = num(n.duration?.seconds);
  if (!/^[\w-]{11}$/.test(id || '') || !(url.includes('/shorts/') || (duration != null && duration <= 180))) return null;
  const cid = n.author?.id && n.author.id.startsWith('UC') ? n.author.id : null;
  return short(id, withExtra({
    caption: n.title?.text ?? null,
    author_id: cid,
    author_username: stripAt(handleFromUrl(n.author?.url)),
    author_url: n.author?.url || channelLink(cid),
    views: count(n.view_count?.text ?? n.views?.text ?? n.short_view_count?.text),
    duration_s: duration,
  }, extra));
}

const VIDEO_NODES = ['ShortsLockupView', 'Video', 'ReelItem'];

// Raw InnerTube JSON (audio pivot, Shorts player).
function* rawFind(node, keys) {
  if (Array.isArray(node)) {
    for (const item of node) yield* rawFind(item, keys);
  } else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if (keys.includes(k) && v && typeof v === 'object' && !Array.isArray(v)) yield v;
      else yield* rawFind(v, keys);
    }
  }
}

function fromRawLockup(vmodel, extra) {
  const ep = vmodel?.onTap?.innertubeCommand?.reelWatchEndpoint || {};
  const id = ep.videoId || String(vmodel.entityId || '').replace(/^shorts-shelf-item-/, '');
  if (!/^[\w-]{11}$/.test(id)) return null;
  const a11y = vmodel.accessibilityText || '';
  const caption = vmodel.overlayMetadata?.primaryText?.content ?? a11y.match(/^(.+), [^,]+ views? - play Short$/)?.[1] ?? null;
  let views = count(vmodel.overlayMetadata?.secondaryText?.content);
  if (views == null) views = count(a11y.match(/, ([^,]+ views?) - play Short$/)?.[1]);
  const cid = channelFromParams(ep.params);
  return short(id, withExtra({ caption, views, author_id: cid, author_url: channelLink(cid) }, extra));
}

function rawContinuation(data) {
  let token = null;
  for (const c of rawFind(data, ['continuationItemRenderer'])) {
    token = c.continuationEndpoint?.continuationCommand?.token || token;
  }
  return token;
}

// (source video id, sound title, sound author) from an audio pivot page header.
function pivotHeader(data) {
  const header = rawFind(data, ['pageHeaderViewModel']).next().value;
  if (!header) return {};
  let id = null;
  for (const e of rawFind(header, ['watchEndpoint', 'reelWatchEndpoint'])) if (!id && e.videoId) id = e.videoId;
  let author = null;
  for (const a of rawFind(header, ['avatarStackViewModel'])) if (!author && a.text?.content) author = a.text.content;
  return { id, title: header.title?.dynamicTextViewModel?.text?.content ?? null, author };
}

// The sound a Short uses, from the Shorts player's audio-pivot panel.
async function reelSound(yt, id) {
  const res = await yt.actions.execute('/reel/reel_item_watch', {
    playerRequest: { videoId: id }, params: 'CAUwAg%3D%3D', disablePlayerResponse: true,
  });
  for (const panel of rawFind(res.data, ['engagementPanelSectionListRenderer'])) {
    if (String(panel.targetId || '').includes('audio-pivot') || JSON.stringify(panel.identifier || {}).includes('audio-pivot')) {
      return pivotHeader(panel);
    }
  }
  return {};
}

function pivotParams(id) {
  const b = (...xs) => Buffer.from(xs);
  return Buffer.concat([b(0xf2, 0x05, 0x2b, 0x0a, 0x29, 0x12, 0x27, 0x0a, 0x0b), Buffer.from(id),
    b(0x12, 0x0b), Buffer.from(id), b(0x1a, 0x0b), Buffer.from(id)]).toString('base64');
}

// ---------- commands ----------

async function search(query, opt) {
  const yt = await client(opt.region);
  const limit = int(opt.limit, 30);
  const delay = Number(opt.delay ?? 1.5);
  const filters = { type: 'shorts' };
  if (opt['upload-date']) filters.upload_date = opt['upload-date'];
  const videosOf = (page) => nodesOf(page.results, VIDEO_NODES).map((n) => fromNode(n)).filter(Boolean);
  let prioritize = 'popularity';
  let page = await yt.search(query, { ...filters, prioritize });
  let found = videosOf(page);
  if (!found.length) {
    // YouTube answered type=shorts + prioritize=popularity with an empty page in testing (Nov 2026).
    log('prioritize=popularity returned no Shorts, retrying with relevance');
    prioritize = 'relevance';
    await sleep(delay);
    page = await yt.search(query, filters);
    found = videosOf(page);
  }
  const out = new Map();
  for (let i = 0; ; i++) {
    for (const v of found) if (!out.has(v.id)) out.set(v.id, v);
    if (out.size >= limit || !page.has_continuation || i + 1 >= maxPages(limit)) break;
    await sleep(delay);
    page = await nextPage(() => page.getContinuation(), out.size);
    if (!page) break;
    found = videosOf(page);
  }
  log(`search ${JSON.stringify(query)} upload_date=${filters.upload_date || 'any'} prioritize=${prioritize}: ${out.size} Shorts`);
  return [...out.values()].slice(0, limit);
}

async function channelId(yt, ref) {
  const r = String(ref).trim();
  if (/^UC[\w-]{22}$/.test(r)) return r;
  const url = /youtube\.com/.test(r) ? (r.includes('://') ? r : `https://${r}`)
    : /^(?:channel|c|user)\/[^/]+/.test(r) ? `https://www.youtube.com/${r}` : `https://www.youtube.com/@${r.replace(/^@/, '')}`;
  const direct = url.match(/\/channel\/(UC[\w-]{22})/);
  if (direct) return direct[1];
  const ep = await yt.resolveURL(url);
  const id = ep?.payload?.browseId;
  if (!id || !id.startsWith('UC')) throw new Error(`could not resolve a channel from ${r}`);
  return id;
}

async function channel(ref, opt) {
  const yt = await client();
  const limit = int(opt.limit, 20);
  const delay = Number(opt.delay ?? 1.5);
  const id = await channelId(yt, ref);
  const ch = await yt.getChannel(id);
  const parts = (ch.header?.content?.metadata?.metadata_rows || [])
    .flatMap((row) => (row.metadata_parts || []).map((p) => p.text?.text || ''));
  const followers = count(parts.find((t) => /subscriber/i.test(t)) ?? ch.header?.subscribers?.text);
  const username = stripAt(parts.find((t) => t.startsWith('@')) || handleFromUrl(ch.metadata?.vanity_channel_url));
  const url = username ? `https://www.youtube.com/@${username}` : channelLink(id);
  if (!ch.has_shorts) throw new Error(`channel ${id} has no Shorts tab`);
  const extra = { author_id: id, author_username: username, author_url: url, author_followers: followers };
  let tab = await ch.getShorts();
  const posts = new Map();
  for (let i = 0; ; i++) {
    for (const n of nodesOf(tab.videos?.length ? tab.videos : tab.contents, VIDEO_NODES)) {
      const v = fromNode(n, extra);
      if (v && !posts.has(v.id)) posts.set(v.id, v);
    }
    if (posts.size >= limit || !tab.has_continuation || i + 1 >= maxPages(limit)) break;
    await sleep(delay);
    tab = await nextPage(() => tab.getContinuation(), posts.size);
    if (!tab) break;
  }
  return { id, username, url, followers, posts: [...posts.values()].slice(0, limit) };
}

function musicFromPanels(panels) {
  // "Music" section of the structured description: rows titled Song / Artist.
  const out = {};
  for (const node of nodesOf(panels, ['InfoRow'])) {
    const title = String(node.title?.text || '').toLowerCase();
    const value = node.default_metadata?.text || node.expanded_metadata?.text || null;
    if (title === 'song' && !out.title) out.title = value;
    if (title === 'artist' && !out.author) out.author = value;
  }
  return out;
}

async function video(ref) {
  const yt = await client();
  const id = videoId(ref);
  const info = await yt.getInfo(id);
  const b = info.basic_info;
  const owner = info.secondary_info?.owner;
  const panels = info.page?.[1]?.engagement_panels || [];
  const commentsPanel = panels.find((p) => (p.panel_identifier || p.target_id) === 'engagement-panel-comments-section');
  const mf = info.page?.[0]?.microformat;
  const username = stripAt(handleFromUrl(owner?.author?.url || b.channel?.url));
  let sound = {};
  try {
    sound = await reelSound(yt, id);
  } catch (e) {
    log(`shorts sound lookup failed: ${e.message}`);
  }
  const music = musicFromPanels(panels);
  return short(id, {
    caption: b.title ?? null,
    author_id: b.channel_id ?? b.channel?.id ?? null,
    author_username: username,
    author_url: username ? `https://www.youtube.com/@${username}` : channelLink(b.channel_id),
    author_followers: count(owner?.subscriber_count?.text),
    views: num(b.view_count),
    likes: num(b.like_count),
    comments: count(commentsPanel?.header?.contextual_info?.text),
    posted_at: isoOrNull(mf?.publish_date || mf?.upload_date),
    duration_s: num(b.duration),
    sound_id: sound.id ?? null,
    sound_title: sound.title ?? music.title ?? null,
    sound_author: sound.author ?? music.author ?? null,
  });
}

async function sound(ref, opt) {
  const yt = await client();
  const id = videoId(ref);
  const limit = int(opt.limit, 30);
  const delay = Number(opt.delay ?? 1.5);
  let source = id;
  try {
    source = (await reelSound(yt, id)).id || id; // a Short that borrows audio pivots only to itself
  } catch (e) {
    log(`sound source lookup failed, pivoting on ${id}: ${e.message}`);
  }
  let res = await yt.actions.execute('/browse', { browseId: 'FEsfv_audio_pivot', params: pivotParams(source) });
  const head = pivotHeader(res.data);
  // The pivot header may name another upload of the same song; keep the id `video` reports.
  const extra = { sound_id: source, sound_title: head.title, sound_author: head.author };
  const out = new Map();
  for (let i = 0; ; i++) {
    for (const vmodel of rawFind(res.data, ['shortsLockupViewModel'])) {
      const v = fromRawLockup(vmodel, extra);
      if (v && !out.has(v.id)) out.set(v.id, v);
    }
    const token = rawContinuation(res.data);
    if (out.size >= limit || !token || i + 1 >= maxPages(limit)) break;
    await sleep(delay);
    res = await nextPage(() => yt.actions.execute('/browse', { continuation: token }), out.size);
    if (!res) break;
  }
  return [...out.values()].slice(0, limit);
}

// YouTube.js needs a JS evaluator to solve the player's n/sig challenges. data.output is a function
// body that already ends with `return process(n, sp, sig)`; it runs in a fresh context without Node globals.
function evaluate(data) {
  const code = `(function () {\n${data.output}\n})()`;
  const sandbox = { URL, URLSearchParams, TextEncoder, TextDecoder };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  return vm.runInNewContext(code, sandbox, { timeout: 20000 });
}

function captionText(body) {
  if (body.trimStart().startsWith('{')) {
    const events = JSON.parse(body).events || [];
    return events.map((e) => (e.segs || []).map((s) => s.utf8 || '').join('')).join(' ').replace(/\s+/g, ' ').trim();
  }
  // timedtext XML (srv1/srv3): keep the text nodes only.
  const entities = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
  return body.replace(/<[^>]+>/g, ' ').replace(/&(amp|lt|gt|quot|#39);/g, (m) => entities[m])
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n))).replace(/\s+/g, ' ').trim();
}

// The caption track to read: the auto-captions in the audio's own language, else the only auto-caption track, else
// subtitles in that language, else the first subtitles. checked is true only when the track's language matched the
// audio language YouTube reports (the same choice and flag as youtube.py's _pick_captions for yt-dlp).
async function captions(info, origLang) {
  const tracks = info.captions?.caption_tracks || [];
  const base = (l) => String(l || '').replace(/^a-/, '').split('-')[0];
  const same = (t) => !!origLang && base(t.language_code) === base(origLang);
  const asr = tracks.filter((t) => t.kind === 'asr');
  const manual = tracks.filter((t) => t.kind !== 'asr');
  let track = asr.find(same) || null;
  let checked = !!track;
  track = track || (asr.length === 1 ? asr[0] : null);
  let source = track ? `auto-captions:${base(track.language_code)}-orig` : null;
  let language = track ? base(track.language_code) : null;
  if (!track && manual.length) {
    track = manual.find(same) || null;
    checked = !!track;
    track = track || manual[0];
    source = `subtitles:${track.language_code}`;
    language = track.language_code;
  }
  if (!track) return { text: null, source: null, language: null, checked: null };
  const url = new URL(track.base_url, 'https://www.youtube.com');
  url.searchParams.set('fmt', 'json3');
  const res = await Platform.shim.fetch(url.toString());
  const body = await res.text();
  if (!res.ok || !body.trim()) throw new Error(`caption fetch returned HTTP ${res.status}, ${body.length} bytes`);
  const text = captionText(body);
  return text ? { text, source, language, checked } : { text: null, source: null, language: null, checked: null };
}

async function download(ref, opt) {
  const id = videoId(ref);
  if (!opt.out) throw new Error('download needs --out DIR');
  fs.mkdirSync(opt.out, { recursive: true });
  Platform.shim.eval = evaluate;
  const yt = await Innertube.create({ lang: 'en', retrieve_player: true });
  // The WEB clients now stream through SABR only (no format URLs); these clients still list plain URLs.
  let info = null;
  let fmts = [];
  for (const c of ['ANDROID_VR', 'TV_SIMPLY', 'IOS']) {
    try {
      info = await yt.getInfo(id, { client: c });
      fmts = (info.streaming_data?.adaptive_formats || []).filter((f) => f.url || f.signature_cipher || f.cipher);
      if (fmts.length) break;
      log(`${c}: no format URLs`);
    } catch (e) {
      log(`${c}: ${e.message}`);
    }
  }
  const side = (f) => Math.min(f.width || 0, f.height || 0);
  const has = (f, s) => (String(f.mime_type || '').includes(s) ? 1 : 0);
  const video = fmts.filter((f) => f.has_video && !f.has_audio && side(f) > 0 && side(f) <= 480)
    .sort((a, b) => side(b) - side(a) || has(b, 'avc1') - has(a, 'avc1') || (b.bitrate || 0) - (a.bitrate || 0))[0];
  const audios = fmts.filter((f) => f.has_audio && !f.has_video);
  const orig = (f) => (f.is_original || f.audio_track?.audio_is_default ? 1 : 0);
  const audio = audios.sort((a, b) => orig(b) - orig(a) || has(b, 'mp4a') - has(a, 'mp4a') || (b.bitrate || 0) - (a.bitrate || 0))[0];
  if (!video || !audio) throw new Error(`no <=480p video plus audio formats (${fmts.length} adaptive formats)`);
  const out = path.join(opt.out, `${id}.mp4`);
  const vpath = path.join(opt.out, `${id}.video.part`);
  const apath = path.join(opt.out, `${id}.audio.part`);
  try {
    for (const [f, file, type] of [[video, vpath, 'video'], [audio, apath, 'audio']]) {
      const stream = await info.download({ itag: f.itag, type, ...(type === 'audio' && audio.language ? { language: audio.language } : {}) });
      await pipeline(Readable.fromWeb(stream), fs.createWriteStream(file));
    }
    const ff = spawnSync(opt.ffmpeg || 'ffmpeg', ['-y', '-loglevel', 'error', '-i', vpath, '-i', apath,
      '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', '-movflags', '+faststart', out], { encoding: 'utf8' });
    if (ff.status !== 0) throw new Error(`ffmpeg merge failed: ${(ff.stderr || ff.error?.message || '').slice(-300)}`);
  } finally {
    for (const f of [vpath, apath]) fs.rmSync(f, { force: true });
  }
  let caps = { text: null, source: null, language: null, checked: null };
  try {
    caps = await captions(info, audio.language);
  } catch (e) {
    log(`captions failed: ${e.message}`);
  }
  return {
    video_path: out, captions_text: caps.text, captions_source: caps.source, captions_language: caps.language,
    captions_language_checked: caps.checked,
  };
}

const COMMANDS = {
  search: (pos, opt) => search(pos[0], opt),
  channel: (pos, opt) => channel(pos[0], opt),
  video: (pos) => video(pos[0]),
  sound: (pos, opt) => sound(pos[0], opt),
  download: (pos, opt) => download(pos[0], opt),
};

const [command, ...rest] = process.argv.slice(2);
const { pos, opt } = parseArgs(rest);
try {
  if (!COMMANDS[command]) throw new Error(`unknown command ${command}; use ${Object.keys(COMMANDS).join(', ')}`);
  if (!pos[0]) throw new Error(`${command} needs an argument`);
  const result = await COMMANDS[command](pos, opt);
  process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
} catch (e) {
  process.stdout.write(`${JSON.stringify({ ok: false, error: `${e.name || 'Error'}: ${e.message}` })}\n`);
  process.exitCode = 1;
}
