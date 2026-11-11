from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

TAIPEI = dt.timezone(dt.timedelta(hours=8))
VIDEO_SHOTS = (0, 6, 11)
RUNWAY_BASE = "https://api.dev.runwayml.com/v1"
PRICE_URL = "https://docs.dev.runwayml.com/guides/pricing.md"
WRITER_PRICE_URL = "https://developers.openai.com/api/docs/models/gpt-5.4-mini.md"
FX_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
WRITER_MODEL = "gpt-5.4-mini"
MAX_PROMPT_BYTES = 20000
MAX_OUTPUT_TOKENS = 5000


class FilmError(RuntimeError):
    pass


class HTTPFailure(FilmError):
    def __init__(self, status, host):
        self.status = status
        super().__init__(f"{host}: HTTP {status}; no paid request was automatically retried")


def today():
    return dt.datetime.now(TAIPEI).date().isoformat()


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def fetch(url, *, method="GET", body=None, headers=None, timeout=60, raw=False):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    merged = {"User-Agent": "AI-Film-Factory/3.0", **(headers or {})}
    if data is not None:
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        raise HTTPFailure(error.code, urllib.parse.urlparse(url).hostname) from None
    except (OSError, TimeoutError):
        # Do not include signed URLs, authorization headers or provider response bodies.
        raise FilmError(f"Network error contacting {urllib.parse.urlparse(url).hostname}") from None
    return value.decode("utf-8-sig") if raw else json.loads(value)


def runway_headers():
    return {"Authorization": "Bearer " + os.environ["RUNWAYML_API_SECRET"],
            "X-Runway-Version": "2024-11-06"}


def positive(value, name, minimum=0):
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise FilmError(f"Invalid {name}")
    return value


def parse_prices(runway, writer, fx_csv):
    def price(pattern, source):
        match = re.search(pattern, source, re.I | re.M)
        if not match:
            raise FilmError("Cannot verify current provider pricing; paid production stopped")
        return positive(match.group(1), "provider price", 0.00001)
    video = price(r"\|\s*`seedance2_mini`\s*\(480p\)\s*\|\s*([\d.]+) credits per second", runway)
    image = price(r"\|\s*`gen4_image`\s*\|\s*([\d.]+) credits per 720p image", runway)
    turbo = price(r"\|\s*`gen4_image_turbo`\s*\|\s*([\d.]+) credits per image", runway)
    credit = price(r"purchased\s+for\s+\$([\d.]+)\s+per credit", runway)
    input_price = price(r"^\| Input \| \$([\d.]+) \| 1M tokens", writer)
    output_price = price(r"^\| Output \| \$([\d.]+) \| 1M tokens", writer)
    rows = list(csv.reader(io.StringIO(fx_csv.lstrip("\ufeff"))))
    usd = next((row for row in rows if row and row[0] == "USD"), None)
    if not usd or len(usd) < 14 or usd[11] != "本行賣出" or rows[0][13] != "即期":
        raise FilmError("Cannot verify Bank of Taiwan USD selling rate")
    rate = positive(usd[13], "exchange rate", 1)
    if rate > 100:
        raise FilmError("Unexpected USD/TWD rate")
    return {"video_credits_per_second": video, "image_credits": image,
            "turbo_image_credits": turbo, "usd_per_credit": credit,
            "writer_input_usd_per_million": input_price,
            "writer_output_usd_per_million": output_price,
            "bot_usd_twd": rate, "usd_twd_guard": max(35, rate * 1.05),
            "checked_at": dt.datetime.now(TAIPEI).isoformat(),
            "sources": [PRICE_URL, WRITER_PRICE_URL, FX_URL]}


def live_prices():
    return parse_prices(fetch(PRICE_URL, raw=True), fetch(WRITER_PRICE_URL, raw=True),
                        fetch(FX_URL, raw=True))


def budget(prices, limit=50, overhead=1):
    limit = positive(limit, "budget", 0.01)
    if limit > 50:
        raise FilmError("This project is authorized for at most NT$50 per film")
    overhead = positive(overhead, "additional allocated cost")
    # Exactly three 5s native-audio clips, one 720p reference still and two edits.
    credits = 15 * prices["video_credits_per_second"] + prices["image_credits"] + 2 * prices["turbo_image_credits"]
    # UTF-8 byte count is a conservative token upper bound; include schema overhead.
    writer_usd = (MAX_PROMPT_BYTES * prices["writer_input_usd_per_million"] +
                  MAX_OUTPUT_TOKENS * prices["writer_output_usd_per_million"]) / 1e6
    usd = credits * prices["usd_per_credit"] + writer_usd
    guarded = usd * prices["usd_twd_guard"] * 1.15 + overhead
    result = {"limit_twd": limit, "reserved_runway_credits": credits,
              "writer_max_usd": round(writer_usd, 6), "tax_fee_buffer": 0.15,
              "additional_allocated_twd": overhead, "estimated_upper_twd": round(guarded, 2),
              "native_video_seconds": 15, "animated_still_seconds": 45,
              "scope": "Runway Dev prepaid API + OpenAI API + tax/FX reserve; not Runway app-plan credits",
              "prices": prices}
    if guarded > limit:
        raise FilmError(f"Cost guard: NT${guarded:.2f} exceeds NT${limit:.2f}; no paid generation")
    return result


def normalized(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()


def story_fingerprint(story):
    return hashlib.sha256(normalized("".join(s["text"] for s in story["shots"])).encode()).hexdigest()


def validate_story(story, history=()):
    for key in ("title", "premise", "ending", "setting"):
        if not isinstance(story.get(key), str) or not 1 <= len(story[key]) <= 180:
            raise FilmError(f"Invalid story field: {key}")
    characters = story.get("characters", [])
    if not 1 <= len(characters) <= 3:
        raise FilmError("Story requires 1–3 characters")
    names = set()
    for character in characters:
        if (not isinstance(character.get("name"), str) or not 1 <= len(character["name"]) <= 8 or
            character.get("voice") not in ("male", "female") or
            not isinstance(character.get("description"), str) or not 1 <= len(character["description"]) <= 140):
            raise FilmError("Invalid character")
        names.add(character["name"])
    if len(names) != len(characters):
        raise FilmError("Character names must be unique")
    if len(story.get("scenes", [])) != 3 or len(story.get("shots", [])) != 12:
        raise FilmError("Need exactly 3 scene references and 12 five-second shots")
    for scene in story["scenes"]:
        if not isinstance(scene, str) or not 1 <= len(scene) <= 450:
            raise FilmError("Invalid scene prompt")
    total = 0
    for index, shot in enumerate(story["shots"]):
        if shot.get("speaker") not in names or type(shot.get("scene")) is not int or shot["scene"] not in range(3):
            raise FilmError(f"Invalid speaker or scene at shot {index+1}")
        line = shot.get("text")
        if not isinstance(line, str) or not 4 <= len(line) <= 24 or any(c in line for c in "\n\r{}\\"):
            raise FilmError(f"Dialogue at shot {index+1} must fit five seconds")
        if not re.search(r"[\u4e00-\u9fff]", line):
            raise FilmError("Dialogue must be Chinese")
        if not isinstance(shot.get("visual_prompt"), str) or not 1 <= len(shot["visual_prompt"]) <= 500:
            raise FilmError("Invalid shot prompt")
        total += len(line)
    if not 120 <= total <= 240:
        raise FilmError("Total dialogue must be 120–240 characters")
    fingerprint = story_fingerprint(story)
    for old in history:
        if normalized(old.get("title", "")) == normalized(story["title"]) or old.get("fingerprint") == fingerprint:
            raise FilmError("Duplicate episode; stopped before video generation")
        # Exact ending rejection complements the writer's semantic avoidance list.
        if normalized(old.get("ending", "")) == normalized(story["ending"]):
            raise FilmError("Repeated ending; stopped before video generation")
    return story


class StateStore:
    """Local state, optionally mirrored before every charge to one dedicated Git branch."""
    def __init__(self, path, remote=False):
        self.path = Path(path)
        self.remote = remote
        self.sha = None
        self.data = {"version": 1, "episodes": {}, "history": []}
        if remote:
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not os.environ.get("GITHUB_TOKEN"):
                raise FilmError("Durable GitHub state requires repository and workflow token")
            self.base = f"https://api.github.com/repos/{repo}"
            self.headers = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            try:
                entry = fetch(self.base + "/contents/production-state.json?ref=film-state", headers=self.headers)
                self.sha = entry["sha"]
                self.data = json.loads(base64.b64decode(entry["content"]))
            except HTTPFailure as error:
                if error.status != 404:
                    raise
                try:
                    fetch(self.base + "/git/ref/heads/film-state", headers=self.headers)
                except HTTPFailure as missing:
                    if missing.status != 404:
                        raise
                    fetch(self.base + "/git/refs", method="POST", headers=self.headers,
                          body={"ref": "refs/heads/film-state", "sha": os.environ["GITHUB_SHA"]})
        elif self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        json_write(self.path, self.data)
        if self.remote:
            body = {"message": "Checkpoint film production [skip ci]", "branch": "film-state",
                    "content": base64.b64encode(json.dumps(self.data, ensure_ascii=False).encode()).decode()}
            if self.sha:
                body["sha"] = self.sha
            result = fetch(self.base + "/contents/production-state.json", method="PUT", body=body, headers=self.headers)
            self.sha = result["content"]["sha"]

    def episode(self, day):
        return self.data["episodes"].setdefault(day, {"tasks": {}, "status": "new"})

    def record_story(self, day, story):
        self.episode(day)["story"] = story
        if not any(item["day"] == day for item in self.data["history"]):
            self.data["history"].append({"day": day, "title": story["title"], "premise": story["premise"],
                "ending": story["ending"], "fingerprint": story_fingerprint(story)})
        self.data["history"] = self.data["history"][-90:]
        for old in sorted(self.data["episodes"])[:-30]:
            del self.data["episodes"][old]
        self.save()
