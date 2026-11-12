#!/usr/bin/env python3
"""AI Film Factory: daily screenplay, durable generation, 60-second subtitled film."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from filmfactory.core import (FilmError, RUNWAY_BASE, StateStore, VIDEO_SHOTS,
    WRITER_MODEL, budget, fetch, json_write, live_prices, runway_headers, today, validate_story)
from filmfactory.media import ensure_tools, render, smoke_test, synthesize
from filmfactory.providers import Runway, new_story, produce_assets

ROOT = Path(__file__).resolve().parent


def doctor(folder, network=False):
    checks = {key: bool(os.environ.get(key)) for key in ("OPENAI_API_KEY", "RUNWAYML_API_SECRET")}
    problems = ["Missing GitHub Secret: " + key for key, present in checks.items() if not present]
    try:
        ensure_tools()
    except FilmError as error:
        problems.append(str(error))
    if not importlib.util.find_spec("edge_tts"):
        problems.append("Install requirements.txt")
    report = {"credentials_present": checks, "ready_for_paid_production": False,
              "problems": problems, "paid_requests_made": 0}
    if network:
        try:
            report["budget"] = budget(live_prices(), overhead=float(os.getenv("ALLOCATED_COST_TWD", "1")))
            if checks["RUNWAYML_API_SECRET"]:
                org = fetch(RUNWAY_BASE + "/organization", headers=runway_headers())
                report["runway_dev_balance"] = org["creditBalance"]
                report["balance_covers_new_film"] = org["creditBalance"] >= report["budget"]["reserved_runway_credits"]
            if checks["OPENAI_API_KEY"]:
                fetch("https://api.openai.com/v1/models/" + WRITER_MODEL,
                      headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
        except FilmError as error:
            problems.append(str(error))
    report["ready_for_paid_production"] = network and not problems
    json_write(folder / "setup-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## 製片設定檢查\n\n")
            output.write("可開始正式製片。\n" if report["ready_for_paid_production"] else
                         "正式製片尚未就緒：\n\n" + "".join("- " + item + "\n" for item in problems))
            output.write("\n本次檢查未呼叫任何付費生成。\n")
    return report


def produce(args, folder):
    if args.date != today():
        raise FilmError("Paid production is limited to the current Taiwan date")
    report = doctor(folder, network=True)
    if not report["ready_for_paid_production"]:
        raise FilmError("Production setup is incomplete; see setup-report.json")
    if os.environ.get("GITHUB_ACTIONS") == "true" and not args.github_state:
        raise FilmError("GitHub runs require durable state before paid generation")
    store = StateStore(ROOT / "output" / "production-state.json", remote=args.github_state)
    episode = store.episode(args.date)
    current = report["budget"]
    already_reserved = sum(task["reserved_credits"] for task in episode["tasks"].values())
    if report["runway_dev_balance"] < current["reserved_runway_credits"] - already_reserved:
        raise FilmError("Runway Dev balance is too low for the remaining tasks")
    if episode.get("budget"):
        for field in ("video_credits_per_second", "image_credits", "turbo_image_credits", "usd_per_credit",
                      "writer_input_usd_per_million", "writer_output_usd_per_million"):
            if episode["budget"]["prices"][field] != current["prices"][field]:
                raise FilmError("Provider price changed during this episode; stopped for budget review")
    episode["budget"] = current
    store.save()
    json_write(folder / "budget.json", current)
    story = new_story(store, args.date, args.topic)
    json_write(folder / "script.json", story)
    placeholders = [{"kind": "native_video" if i in VIDEO_SHOTS else "still"} for i in range(12)]
    asyncio.run(synthesize(story, placeholders, folder))
    assets = produce_assets(story, folder, Runway(store, args.date), current["prices"])
    target, rendered = render(story, assets, folder)
    episode["status"] = "rendered_pending_human_quality_review"
    episode["render_report"] = rendered
    store.save()
    json_write(folder / "cost-report.json", {"budget": current,
        "writer_usage": episode.get("writer_usage", {}), "tasks": episode["tasks"],
        "invoice_reconciled": False, "note": "Reservations include uncertain/failed tasks; invoice may differ."})
    print("Rendered:", target)
    print("15 seconds native dialogue video + 45 seconds animated stills; quality review pending.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "produce", "demo", "render"], nargs="?", default="check")
    parser.add_argument("--date", default=today())
    parser.add_argument("--topic", default=os.getenv("FILM_TOPIC", ""))
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--github-state", action="store_true")
    parser.add_argument("--story", type=Path, default=ROOT / "examples" / "sample-story.json")
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    import datetime
    if datetime.date.fromisoformat(args.date).isoformat() != args.date:
        raise FilmError("Date must be YYYY-MM-DD")
    folder = (args.output or ROOT / "output" / args.date).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "check":
            doctor(folder, args.network)
        elif args.mode == "produce":
            produce(args, folder)
        elif args.mode == "demo":
            story = validate_story(json.loads(args.story.read_text(encoding="utf-8")))
            target, _ = smoke_test(story, folder)
            print("TECHNICAL TEST ONLY — not a generated human film:", target)
        else:
            if not args.assets:
                raise FilmError("render requires --assets")
            story = validate_story(json.loads(args.story.read_text(encoding="utf-8")))
            assets = json.loads(args.assets.read_text(encoding="utf-8"))
            target, _ = render(story, assets, folder)
            print("Rendered:", target)
    except Exception as error:
        message = str(error) if isinstance(error, FilmError) else f"{type(error).__name__}; inspect local stage inputs"
        json_write(folder / "failure.json", {"status": "blocked_or_failed", "reason": message,
                   "film_completed": False, "date": args.date})
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
