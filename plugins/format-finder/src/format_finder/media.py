"""Download one video, cut frames weighted toward the hook, and get captions or a local Whisper transcript."""
import functools
import importlib
import re
import subprocess
import time
from pathlib import Path

from . import settings
from .common import FF_HOME, AllRoutesFailed, RouteError, log

HOOK_TIMES = (0.5, 1.5, 3.0)
FRAME_WIDTH = 480
# Whisper's own defaults for a segment it should not trust (openai-whisper transcribe(): logprob_threshold -1.0,
# no_speech_threshold 0.6). Music-only videos come back as short repeated lines under these.
LOW_LOGPROB = -1.0
NO_SPEECH = 0.6
# YouTube's caption codes that Whisper writes differently (yt-dlp's YouTube language list has iw and fil; faster-whisper
# has he, jw and tl), so a caption track in the language Whisper hears is not marked as a mismatch.
WHISPER_CODES = {"iw": "he", "jv": "jw", "fil": "tl"}


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args):
    # stdin=DEVNULL: ffmpeg reads keys from stdin while it runs, which would eat input meant for the caller (a shell
    # `while read` loop over a list of videos lost the start of its next line).
    return subprocess.run([ffmpeg_exe(), "-hide_banner", *args], capture_output=True, text=True, errors="replace",
                          stdin=subprocess.DEVNULL)


def _seconds(h, m, s):
    return int(h) * 3600 + int(m) * 60 + float(s)


def probe(video_path):
    """{"duration_s", "has_video", "has_audio"} parsed from `ffmpeg -i` stderr (there is no ffprobe)."""
    err = _run(["-i", str(video_path)]).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = _seconds(*m.groups()) if m else None
    if duration is None and "Input #" in err:
        # No duration in the header (e.g. a streamed webm): copy the streams to nowhere and read the last timestamp.
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", _run(["-i", str(video_path), "-c", "copy", "-f", "null", "-"]).stderr)
        duration = _seconds(*times[-1]) if times else None
    return {
        "duration_s": duration,
        "has_video": bool(re.search(r"Stream #[^:]+:\d+[^:]*: Video:", err)),
        "has_audio": bool(re.search(r"Stream #[^:]+:\d+[^:]*: Audio:", err)),
    }


def frame_times(duration, n):
    """n timestamps: the hook moments 0.5s, 1.5s and 3s when the video is long enough, the rest evenly to the end."""
    if not n or n <= 0:
        return []
    if not duration:
        return list(HOOK_TIMES[:n])
    fit = [t for t in HOOK_TIMES if t <= duration - 0.1]
    hooks = fit[:n]
    rest = n - len(hooks)
    # Spread after the last hook; a video too short for all three hooks gets the rest spread over its whole length.
    start = fit[-1] if len(fit) == len(HOOK_TIMES) else 0.0
    end = max(duration - 0.5, start + (duration - start) * 0.9)
    spread = [start + (end - start) * (i + 1) / rest for i in range(rest)]
    return sorted({round(t, 2) for t in hooks + spread})


def cut_frames(video_path, out_dir, n, duration=None):
    """Cut n JPEG frames, 480px wide, into out_dir. Returns [{"path", "time_s"}] for the frames ffmpeg produced."""
    if not Path(video_path).is_file():
        raise FileNotFoundError(f"video file not found: {video_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if duration is None:
        duration = probe(video_path)["duration_s"]
    stem = Path(video_path).stem
    frames = []
    for i, t in enumerate(frame_times(duration, n), 1):
        path = out_dir / f"{stem}_{i:02d}_{t:.2f}s.jpg"
        path.unlink(missing_ok=True)
        proc = _run(["-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video_path), "-frames:v", "1",
                     "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", "-update", "1", str(path)])
        if proc.returncode == 0 and path.is_file() and path.stat().st_size:
            frames.append({"path": str(path), "time_s": t})
        else:
            log(f"[frames] no frame at {t}s: {proc.stderr.strip()[-300:] or 'past the end'}")
    return frames


def load_audio(video_path):
    """Mono 16 kHz float32 samples decoded by the ffmpeg binary.

    faster-whisper's own decoder (PyAV) breaks on av 19 (`metadata_errors` was removed), so Whisper gets an array.
    """
    import numpy as np
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-vn",
                           "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True,
                          stdin=subprocess.DEVNULL)  # see _run
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode audio: {proc.stderr.decode(errors='replace').strip()[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


@functools.lru_cache(maxsize=2)
def _whisper(model_name):
    from faster_whisper import WhisperModel
    return WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(FF_HOME / "models"))


def transcribe(video_path, model_name=None):
    """Local Whisper transcript {"text", "segments": [{"start", "end", "text", "avg_logprob", "no_speech_prob"}],
    "language", "language_probability", "speech", "low_confidence", "source"}.

    None when the file has no audio stream. Text is "" and speech False when Whisper heard no speech. low_confidence
    is True when the segments' mean avg_logprob is under LOW_LOGPROB or their mean no_speech_prob is over NO_SPEECH:
    the text may be made up (music-only videos).
    """
    if not probe(video_path)["has_audio"]:
        return None
    model_name = model_name or settings.get("whisper_model")
    started = time.monotonic()
    segments, info = _whisper(model_name).transcribe(load_audio(video_path), vad_filter=True)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
             "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3)} for s in segments]
    log(f"[whisper:{model_name}] {len(segs)} segments, language {info.language}, {time.monotonic() - started:.1f}s")
    low = None
    if segs:
        logprob = sum(s["avg_logprob"] for s in segs) / len(segs)
        no_speech = sum(s["no_speech_prob"] for s in segs) / len(segs)
        low = logprob < LOW_LOGPROB or no_speech > NO_SPEECH
    return {"text": " ".join(s["text"] for s in segs if s["text"]), "segments": segs,
            "language": info.language, "language_probability": round(info.language_probability, 3),
            "speech": bool(segs), "low_confidence": low, "source": f"whisper:{model_name}"}


def audio_language(video_path, model_name=None):
    """(language, probability) Whisper detects in the audio, without transcribing it; (None, None) with no audio."""
    if not probe(video_path)["has_audio"]:
        return None, None
    model_name = model_name or settings.get("whisper_model")
    _, info = _whisper(model_name).transcribe(load_audio(video_path))  # detection runs here; segments stay unread
    return info.language, round(info.language_probability, 3)


TRANSCRIPT_FIELDS = ("transcript", "transcript_source", "segments", "transcript_language", "language_probability",
                     "speech", "low_confidence", "captions_language_checked")


def _transcript(download, video_path, use_whisper, errors):
    """The TRANSCRIPT_FIELDS: platform captions first, else Whisper when allowed. captions_language_checked is the
    download's: True when the captions' language matched the audio language YouTube reports, False when it was not
    checked, None when the route does not say (and for a Whisper transcript). Unchecked captions get Whisper's
    language detection too, when Whisper is allowed: audio_language, audio_language_probability and
    language_mismatch (absent when the detection did not run)."""
    out = dict.fromkeys(TRANSCRIPT_FIELDS)
    if download.get("captions_text"):
        out.update(transcript=download["captions_text"], transcript_source=download.get("captions_source"),
                   transcript_language=download.get("captions_language"),
                   captions_language_checked=download.get("captions_language_checked"))
        if download.get("captions_language_checked") is False and use_whisper and video_path:
            try:
                lang, prob = audio_language(video_path)
            except Exception as exc:  # noqa: BLE001 - the captions are still there
                errors.append({"route": "whisper", "error": f"{type(exc).__name__}: {exc}"})
                return out
            caption_lang = str(download.get("captions_language") or "").split("-")[0].lower() or None
            caption_lang = WHISPER_CODES.get(caption_lang, caption_lang)
            out.update(audio_language=lang, audio_language_probability=prob,
                       language_mismatch=bool(lang and caption_lang and lang != caption_lang))
        return out
    if not use_whisper or not video_path:
        return out
    try:
        heard = transcribe(video_path)
    except Exception as exc:  # noqa: BLE001 - frames are still useful without a transcript
        errors.append({"route": "whisper", "error": f"{type(exc).__name__}: {exc}"})
        log(f"[whisper] failed: {type(exc).__name__}: {exc}")
        return out
    if heard is None:
        return dict(out, speech=False)  # no audio stream
    out.update(transcript=heard["text"], transcript_source=heard["source"], segments=heard["segments"],
               transcript_language=heard["language"], language_probability=heard["language_probability"],
               speech=heard["speech"], low_confidence=heard["low_confidence"])
    return out


def fetch(platform, ref, out_dir, frames=None, transcribe=True):
    """Download one video with the platform's routes, then cut frames and get captions or a Whisper transcript."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {"ok": False, "command": "media", "platform": platform, "ref": ref, "route": None, "errors": [],
           "video_path": None,
           "frames": [], "duration_s": None, **dict.fromkeys(TRANSCRIPT_FIELDS)}
    try:
        env = importlib.import_module(f"format_finder.{platform}").download(ref, str(out_dir))
    except AllRoutesFailed as exc:
        out["errors"] = exc.errors
        return out
    except (ImportError, RouteError) as exc:
        out["errors"] = [{"route": None, "error": f"{type(exc).__name__}: {exc}"}]
        return out

    download = env.get("result") or {}
    errors = list(env.get("errors") or [])
    video_path = download.get("video_path")
    if video_path and not Path(video_path).is_file():
        errors.append({"route": env.get("route"), "error": f"video file not found: {video_path}"})
        video_path = None
    if not video_path:
        errors.append({"route": env.get("route"), "error": "download returned no video file"})

    if video_path:
        info = probe(video_path)
        duration = info["duration_s"]
        out["duration_s"] = round(duration, 2) if duration is not None else None
        n = frames if frames is not None else settings.get("frames_per_video")
        if info["has_video"]:
            try:
                out["frames"] = cut_frames(video_path, out_dir / "frames", n, duration=duration)
            except OSError as exc:
                errors.append({"route": "frames", "error": f"{type(exc).__name__}: {exc}"})

    heard = _transcript(download, video_path, transcribe, errors)
    out.update(heard, ok=bool(video_path or heard["transcript"]), route=env.get("route"), errors=errors,
               video_path=video_path)
    return out
