from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess

from .core import FilmError, json_write, validate_story


def run(*args, cwd=None):
    command = [str(a) for a in args]
    if command[0] == "ffmpeg":
        command[1:1] = ["-nostdin", "-xerror"]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise FilmError(f"{args[0]} failed: {result.stderr[-1800:]}")
    return result.stdout


def probe(path):
    return json.loads(run("ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path))


def duration(path):
    return float(probe(path)["format"]["duration"])


def ensure_tools():
    for tool in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(tool):
            raise FilmError(f"Missing system dependency: {tool}")
    family = run("fc-match", "-f", "%{family}", "Noto Sans CJK TC")
    if "Noto Sans CJK" not in family:
        raise FilmError("Install fonts-noto-cjk before rendering Chinese subtitles")


def reference_png(source, target):
    run("ffmpeg", "-v", "error", "-y", "-i", source, "-frames:v", "1", "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280", target)


def stamp(seconds, ass=False):
    if ass:
        units = round(seconds * 100)
        return f"{units//360000}:{units//6000%60:02}:{units//100%60:02}.{units%100:02}"
    units = round(seconds * 1000)
    return f"{units//3600000:02}:{units//60000%60:02}:{units//1000%60:02},{units%1000:03}"


def subtitles(story, folder, demo=False):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK TC,38,&H00FFFFFF,&H00FFFFFF,&H00151515,&H90000000,1,0,0,0,100,100,0,0,1,3,1,2,52,52,150,1
Style: Label,Noto Sans CJK TC,28,&H0000FFFF,&H00FFFFFF,&H00151515,&H90000000,1,0,0,0,100,100,0,0,1,2,1,8,30,30,60,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ass, srt = [header], []
    if demo:
        ass.append("Dialogue: 1,0:00:00.00,0:01:00.00,Label,,0,0,0,,技術測試畫面・非真人成片\n")
    for index, shot in enumerate(story["shots"]):
        start, end = 5 * index + 0.15, 5 * (index + 1) - 0.1
        line = shot["speaker"] + "：" + shot["text"]
        line = line.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")
        wrapped = r"\N".join(line[i:i+15] for i in range(0, len(line), 15))
        ass.append(f"Dialogue: 0,{stamp(start,True)},{stamp(end,True)},Default,,0,0,0,,{wrapped}\n")
        srt.append(f"{index+1}\n{stamp(start)} --> {stamp(end)}\n{line}\n\n")
    (folder / "subtitles.ass").write_text("".join(ass), encoding="utf-8")
    (folder / "subtitles.srt").write_text("".join(srt), encoding="utf-8")


async def synthesize(story, assets, folder):
    import edge_tts
    voices = {c["name"]: "zh-TW-YunJheNeural" if c["voice"] == "male" else "zh-TW-HsiaoChenNeural"
              for c in story["characters"]}
    for index, (shot, asset) in enumerate(zip(story["shots"], assets, strict=True)):
        if asset["kind"] == "native_video":
            continue  # Native lip-synced dialogue must never be replaced by TTS.
        output = folder / f"voice-{index:02}.wav"
        if output.exists() and abs(duration(output) - 5) < 0.02:
            asset["audio"] = str(output.resolve())
            continue
        raw = folder / f"voice-{index:02}.mp3"
        await edge_tts.Communicate(shot["text"], voices[shot["speaker"]], rate="+8%").save(str(raw))
        seconds = duration(raw)
        speed = max(1.0, seconds / 4.6)
        if speed > 1.35:
            raise FilmError(f"Shot {index+1} speech too long for natural delivery; no truncation")
        run("ffmpeg", "-v", "error", "-y", "-i", raw, "-af",
            f"atempo={speed:.5f},adelay=150:all=1,apad,atrim=duration=5",
            "-ar", "48000", "-ac", "2", output)
        asset["audio"] = str(output.resolve())


def verify_native(path):
    info = probe(path)
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise FilmError("Native dialogue clip has no audio; will not replace it with dubbed speech")
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video or not 4.85 <= float(info["format"]["duration"]) <= 5.5:
        raise FilmError("Native clip does not match the five-second timeline")
    result = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af", "volumedetect",
                             "-f", "null", "-"], capture_output=True, text=True)
    match = re.search(r"max_volume: (-?[\d.]+) dB", result.stderr)
    if result.returncode or not match or float(match.group(1)) < -55:
        raise FilmError("Native clip audio is silent or unreadable")


def render(story, assets, folder, *, use_tts=True, demo=False, size=(720,1280)):
    validate_story(story)
    ensure_tools()
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if len(assets) != 12:
        raise FilmError("Exactly 12 asset entries are required")
    for asset in assets:
        if asset["kind"] not in ("native_video", "still") or not Path(asset["path"]).is_file():
            raise FilmError("Invalid media manifest")
        if asset["kind"] == "native_video":
            verify_native(asset["path"])
    if use_tts:
        asyncio.run(synthesize(story, assets, folder))
    subtitles(story, folder, demo)
    json_write(folder / "script.json", story)
    width, height = size
    inputs, filters, timeline = [], [], []
    input_index = 0
    for index, asset in enumerate(assets):
        source = Path(asset["path"]).resolve()
        inputs.extend(["-i", source])
        visual_input = input_index
        input_index += 1
        fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        if asset["kind"] == "native_video":
            visual = fit + ",fps=24,setsar=1,tpad=stop_mode=clone:stop_duration=0.2"
            audio_input = visual_input
        else:
            if not asset.get("audio") or not Path(asset["audio"]).exists():
                raise FilmError(f"Missing speech for still shot {index+1}")
            # Oversample before zoompan to avoid jitter; each still is explicitly 5 seconds.
            visual = (f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
                    f"crop={width*2}:{height*2},zoompan=z='1.03+on*0.0005':"
                    f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=120:s={width}x{height}:fps=24,setsar=1")
            inputs.extend(["-i", Path(asset["audio"]).resolve()])
            audio_input = input_index
            input_index += 1
        filters.append(f"[{visual_input}:v]{visual},trim=duration=5,setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[{audio_input}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                       f"apad,atrim=duration=5,asetpts=PTS-STARTPTS[a{index}]")
        timeline.append(f"[v{index}][a{index}]")
    filters.append("".join(timeline) + "concat=n=12:v=1:a=1[vout][aout]")
    run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex_threads", "1", "-filter_complex",
        ";".join(filters), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
        "-threads", "2", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", "60", "-movflags", "+faststart", "film_clean.mp4", cwd=folder)
    run("ffmpeg", "-v", "error", "-y", "-i", "film_clean.mp4", "-vf", "ass=subtitles.ass",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", "60", "-movflags", "+faststart", "film.mp4", cwd=folder)
    info = probe(folder / "film.mp4")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if (abs(float(info["format"]["duration"]) - 60) > 0.15 or video["codec_name"] != "h264" or
        video["width"] * 16 != video["height"] * 9 or not audio or audio["codec_name"] != "aac"):
        raise FilmError("Output failed duration, aspect ratio or codec validation")
    native_seconds = 5 * sum(a["kind"] == "native_video" for a in assets)
    report = {"status": "technical_demo" if demo else "rendered_pending_human_quality_review",
              "duration_seconds": float(info["format"]["duration"]),
              "width": video["width"], "height": video["height"],
              "native_video_seconds": native_seconds, "animated_still_seconds": 60-native_seconds,
              "audio_policy": "native video audio preserved; TTS only on still shots",
              "subtitles": "ASS burned in; SRT also included (5-second beat timing)",
              "quality_note": "Dialogue accuracy, lip sync, acting and identity require visual/auditory review"}
    json_write(folder / "render-report.json", report)
    return folder / "film.mp4", report


def smoke_test(story, folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    native, still, tone = folder / "test-native.mp4", folder / "test-still.png", folder / "test-tone.wav"
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5", "-t", "5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", native)
    run("ffmpeg", "-v", "error", "-y", "-i", native, "-frames:v", "1", still)
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=5",
        "-ar", "48000", "-ac", "2", tone)
    assets = [{"kind": "native_video", "path": str(native)} if i in (0,6,11) else
              {"kind": "still", "path": str(still), "audio": str(tone)} for i in range(12)]
    return render(story, assets, folder, use_tts=False, demo=True, size=(360,640))
