from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

from .core import (FilmError, MAX_OUTPUT_TOKENS, MAX_PROMPT_BYTES, RUNWAY_BASE,
                   WRITER_MODEL, fetch, runway_headers, validate_story)


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
STORY_SCHEMA = obj({
    "title": TEXT, "premise": TEXT, "ending": TEXT, "setting": TEXT,
    "characters": {"type": "array", "items": obj({"name": TEXT, "description": TEXT,
        "voice": {"type": "string", "enum": ["male", "female"]}})},
    "scenes": {"type": "array", "items": TEXT},
    "shots": {"type": "array", "items": obj({"speaker": TEXT, "text": TEXT,
        "scene": {"type": "integer", "enum": [0, 1, 2]}, "visual_prompt": TEXT})},
})


def new_story(store, day, topic=""):
    episode = store.episode(day)
    if episode.get("story"):
        return validate_story(episode["story"])
    if episode.get("writer_status"):
        raise FilmError("Previous screenplay request did not complete; will not charge again automatically")
    instructions = """你是台灣黑色幽默短劇編劇。創作全新60秒真人職場諷刺短劇，繁體中文、台灣口吻。
有開場鉤子、因果推進、鋪梗、最後反轉，荒謬但非隨機拼湊。不可重複提供的歷史故事、笑點、結尾。
輸出指定JSON。title/premise/ending/setting每欄不超過180字。角色1至3人，名字最多8字，description用英文描述外表與固定服裝，最多140字元；voice為male或female。
scenes為3個英文場景劇照提示，每項最多450字元，保持同一組演員與服裝，改變構圖。每張可作數個鏡頭的參考，勿要求畫面文字。
shots恰好12項，每項5秒。每項text為單一角色一句台詞，建議10至18字、上限24字含標點，總台詞120至240字，確實能自然說完。
speaker必須是角色名字，scene為0/1/2，visual_prompt為最多500字元的英文真人畫面描述。
第1、7、12鏡是原生中文開口對話動態鏡頭；其他鏡頭用劇照運鏡與畫外角色對話，安排背影、道具、反應鏡頭，避免假裝靜態嘴型在說話。
不要旁白解說；人物真的互相交談。第12鏡只能一位角色說結尾，不能把兩位角色台詞塞進同一句。
對白不要反斜線、大括號或換行。作品9:16、真實電影感。"""
    history = store.data["history"][-10:]
    prompt = json.dumps({"date": day, "topic": topic[:300] or "全新荒謬職場事件",
                         "avoid_previous": history}, ensure_ascii=False)
    body = {"model": WRITER_MODEL, "messages": [{"role": "system", "content": instructions},
            {"role": "user", "content": prompt}], "reasoning_effort": "none",
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "daily_film", "strict": True, "schema": STORY_SCHEMA}}}
    if len(json.dumps(body, ensure_ascii=False).encode()) + 512 > MAX_PROMPT_BYTES:
        raise FilmError("Writer prompt exceeds reserved input budget")
    episode["writer_status"] = "submitting"
    store.save()  # Durable intent before POST: ambiguous network failures cannot double-charge.
    result = fetch("https://api.openai.com/v1/chat/completions", method="POST", body=body,
                   headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}, timeout=180)
    episode["writer_status"] = "returned"
    episode["writer_usage"] = result.get("usage", {})
    store.save()
    choice = result["choices"][0]
    if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
        raise FilmError("Screenplay incomplete or refused; video generation was not started")
    try:
        story = validate_story(json.loads(choice["message"]["content"]), store.data["history"])
    except (ValueError, KeyError, TypeError):
        raise FilmError("Writer returned an invalid screenplay; video generation was not started") from None
    store.record_story(day, story)
    return story


def download(url, path):
    if urllib.parse.urlparse(url).scheme != "https":
        raise FilmError("Media must use HTTPS")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AI-Film-Factory/3.0"})
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as output:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 250 * 1024 * 1024:
                    raise FilmError("Unexpectedly large media download")
                output.write(chunk)
        if size < 1000:
            raise FilmError("Incomplete media download")
        tmp.replace(path)
    except (OSError, TimeoutError):
        tmp.unlink(missing_ok=True)
        raise FilmError("Media download failed; task retained for retry without new generation") from None
    return path


class Runway:
    def __init__(self, store, day):
        self.store, self.day = store, day

    def generate(self, name, endpoint, body, cost, target):
        episode = self.store.episode(self.day)
        tasks = episode["tasks"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        entry = tasks.get(name)
        if entry and entry["request_hash"] != digest:
            raise FilmError("Saved generation parameters differ; manual review required before any new charge")
        if entry is None:
            reserved = sum(item["reserved_credits"] for item in tasks.values())
            if reserved + cost > episode["budget"]["reserved_runway_credits"]:
                raise FilmError("Per-film credit reservation exceeded")
            entry = {"request_hash": digest, "reserved_credits": cost, "status": "submitting"}
            tasks[name] = entry
            self.store.save()
            result = fetch(RUNWAY_BASE + endpoint, method="POST", body=body, headers=runway_headers())
            if not result.get("id"):
                raise FilmError("Runway returned no task ID; no automatic resubmission")
            entry.update(task_id=result["id"], status="submitted")
            self.store.save()
        if not entry.get("task_id"):
            raise FilmError("Uncertain previous Runway submission; stopped to avoid duplicate billing")
        target = Path(target)
        if target.exists() and entry.get("sha256") == hashlib.sha256(target.read_bytes()).hexdigest():
            return target
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            task = fetch(RUNWAY_BASE + "/tasks/" + entry["task_id"], headers=runway_headers())
            status = task["status"]
            if status == "SUCCEEDED":
                outputs = task.get("output", [])
                if not outputs:
                    raise FilmError("Runway succeeded without a media output")
                download(outputs[0], target)
                entry.update(status="succeeded", sha256=hashlib.sha256(target.read_bytes()).hexdigest())
                self.store.save()
                return target
            if status in ("FAILED", "CANCELED", "CANCELLED"):
                entry["status"] = status.lower()
                self.store.save()
                raise FilmError(f"Runway task {status}; no additional paid attempt")
            time.sleep(10)
        raise FilmError("Runway timeout; retry resumes the same task ID without resubmitting")


def image_uri(path):
    data = Path(path).read_bytes()
    if len(data) > 4 * 1024 * 1024:
        raise FilmError("Reference image too large for data URI")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def produce_assets(story, folder, runway, prices):
    from .core import VIDEO_SHOTS
    from .media import reference_png
    folder = Path(folder)
    cast = "; ".join(c["description"] for c in story["characters"])
    references = []
    for index, scene in enumerate(story["scenes"]):
        prompt = ("Photorealistic Taiwanese dark comedy film still, natural skin, cinematic lighting. " +
                  cast + ". " + scene + ". No words, no subtitles. Keep identical actors and costumes.")
        if len(prompt.encode("utf-16-le")) // 2 > 1000:
            raise FilmError("Image prompt exceeds provider limit")
        body = {"model": "gen4_image" if index == 0 else "gen4_image_turbo",
                "ratio": "720:1280", "promptText": prompt}
        if index:
            body["referenceImages"] = [{"uri": image_uri(references[0]), "tag": "cast"}]
        source = runway.generate(f"scene-{index}", "/text_to_image", body,
            prices["image_credits"] if index == 0 else prices["turbo_image_credits"], folder / f"scene-{index}.source")
        target = folder / f"scene-{index}.png"
        reference_png(source, target)
        references.append(target)
    assets = []
    for index, shot in enumerate(story["shots"]):
        if index in VIDEO_SHOTS:
            prompt = ("5-second continuous shot. Photorealistic Taiwanese dark comedy, same actors and costumes as reference. " +
                      cast + ". " + shot["visual_prompt"] + ". Only " + shot["speaker"] +
                      " speaks, in natural Taiwanese Mandarin, exactly: 「" + shot["text"] +
                      "」. Start after 0.3 seconds, finish before 4.5 seconds. Accurate natural lip movement. " +
                      "No narrator, no subtitles, no music. Quiet room ambience. No extra dialogue.")
            body = {"model": "seedance2_mini", "promptText": prompt, "duration": 5,
                    "ratio": "496:864", "audio": True,
                    "references": [{"uri": image_uri(references[shot["scene"]])}]}
            video = runway.generate(f"shot-{index:02}", "/text_to_video", body,
                5 * prices["video_credits_per_second"], folder / f"native-{index:02}.mp4")
            assets.append({"kind": "native_video", "path": str(video.resolve())})
        else:
            assets.append({"kind": "still", "path": str(references[shot["scene"]].resolve())})
    return assets
