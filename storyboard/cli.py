"""python -m storyboard [--spec story.json] [--config config.json] <command> ...   (see README.md)"""
import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import images, qa, render
from .spec import Story


def load(args) -> tuple[Story, Path, dict]:
    story = Story(args.spec)
    cfg, base = {}, Path.cwd()
    if args.config:
        cfg_path = Path(args.config).resolve()
        cfg, base = json.loads(cfg_path.read_text()), cfg_path.parent
    work = (base / cfg["work"]).resolve() if cfg.get("work") else Path.cwd() / "work" / story.name
    return story, work, cfg | {"_base": base}


def renderer(story: Story, work: Path, cfg: dict) -> render.Render:
    if not cfg.get("nodes"):
        sys.exit("this command needs --config with at least one render node (see config.example.json)")
    nodes = [render.Node(**n) for n in cfg["nodes"]]
    return render.Render(story, work, nodes, cfg.get("max_seeds", 3))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m storyboard", description=__doc__)
    p.add_argument("--spec", default=os.environ.get("STORYBOARD_SPEC"), help="story spec JSON ($STORYBOARD_SPEC)")
    p.add_argument("--config", default=os.environ.get("STORYBOARD_CONFIG"), help="nodes/work config ($STORYBOARD_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the spec and print the scenario table")
    c = sub.add_parser("prompt", help="print the Codex prompt + attachments for one still")
    c.add_argument("style"), c.add_argument("frame")
    c = sub.add_parser("h3-task", help="print the WanGP task JSON for one segment")
    c.add_argument("style"), c.add_argument("a"), c.add_argument("b")
    c = sub.add_parser("assets", help="generate the spec's assets (character / prop sheets)")
    c.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("stills", help="generate missing stills (master edits, then style repaints)")
    c.add_argument("--styles", help="comma list (default: all)")
    c.add_argument("--only", help="comma list of frame ids")
    c.add_argument("--workers", type=int, default=6)
    c.add_argument("--dry-run", action="store_true", help="print prompts, generate nothing")
    c = sub.add_parser("sheet", help="contact sheet of a style's stills")
    c.add_argument("style")
    c = sub.add_parser("reject", help="delete a still (+ dependants, clips, queued tasks); `stills` remakes it")
    c.add_argument("style"), c.add_argument("frame")
    sub.add_parser("pump", help="one loop step: sync, pull, queue, stitch, status")
    c = sub.add_parser("run", help="pump forever")
    c.add_argument("--every", type=int, default=300, help="seconds between pumps")
    sub.add_parser("status", help="print STATUS")
    c = sub.add_parser("redo", help="reject a clip; the next pump renders the next seed")
    c.add_argument("style"), c.add_argument("seg", help="<from>__<to>")
    sub.add_parser("requeue", help="pull unstarted tasks back so the next pump re-queues by priority")
    sub.add_parser("stop", help="pause: persisted flag (even a running `run` loop queues nothing) + pull back "
                                "unstarted tasks; running tasks finish and are still collected")
    sub.add_parser("resume", help="clear the pause flag; the next pump queues again")
    c = sub.add_parser("publish", help="copy reviewed videos to the publish dir (+ rclone)")
    c.add_argument("items", help="<style>/<scenario>,...")
    c = sub.add_parser("qa", help="score every clip against its endpoint stills; write 0.5 s review grids per video")
    c.add_argument("style")
    c.add_argument("--no-grids", action="store_true")
    args = p.parse_args(argv)
    if not args.spec:
        p.error("--spec (or $STORYBOARD_SPEC) is required")
    story, work, cfg = load(args)

    if args.cmd == "check":
        for sc, seq in story.scenarios.items():
            secs = sum(story.segments[pr]["frames"] for pr in story.pairs(sc)) / story.h3["fps"]
            print(f"{sc:12} {len(seq)} frames  ~{secs:4.1f}s  {' > '.join(seq)}")
        print(f"OK: {len(story.frames)} frames, {len(story.segments)} segments, styles {', '.join(story.styles)}; "
              f"work dir {work}")
    elif args.cmd == "prompt":
        prompt, attach = images.Stills(story, work, dry_run=True).plan(args.style, args.frame)
        print("\n".join(f"-i {p}" for p, _ in attach) + "\n\n" + prompt)
    elif args.cmd == "h3-task":
        nodes = [render.Node(**n) for n in cfg.get("nodes", [])]
        n = nodes[0] if nodes else render.Node("example", "user@gpu-host", "/home/user/sbq")
        prio = story.priorities().get((args.style, args.a, args.b), 0)
        print(json.dumps(render.Render(story, work, [n]).task(n, args.style, args.a, args.b, 1, prio), indent=1))
    elif args.cmd == "assets":
        st = images.Stills(story, work, args.dry_run)
        bad = [a for a in story.assets if not st.make_asset(a)]
        if not args.dry_run:
            print("assets:", "all ok" if not bad else f"FAILED {bad}")
    elif args.cmd == "stills":
        styles = args.styles.split(",") if args.styles else list(story.styles)
        only = args.only.split(",") if args.only else None
        failed = images.Stills(story, work, args.dry_run).run(styles, only, args.workers)
        return 1 if failed and not args.dry_run else 0
    elif args.cmd == "sheet":
        print(images.sheet(story, work, args.style))
    elif args.cmd == "reject":
        gone = images.Stills(story, work).reject(args.style, args.frame)
        print("deleted stills:", ", ".join(gone) or "none")
        if cfg.get("nodes") and gone:
            renderer(story, work, cfg).drop(gone)
    elif args.cmd == "pump":
        print("\n".join(renderer(story, work, cfg).pump()) or "nothing new")
    elif args.cmd == "run":
        while True:
            print(f"=== {time.strftime('%F %T')}", flush=True)
            try:
                # re-read the spec every cycle: an edited path or scene must reach the next queued task without
                # restarting the loop (tasks already queued keep the prompt they were queued with)
                r = renderer(Story(args.spec), work, cfg)
                for line in r.pump():
                    print(line, flush=True)
            except Exception:          # a flaky node must not end an overnight loop
                traceback.print_exc()
            time.sleep(args.every)
    elif args.cmd == "status":
        r = renderer(story, work, cfg)
        print(r.write_status(r.load()))
    elif args.cmd == "redo":
        renderer(story, work, cfg).redo(args.style, args.seg)
    elif args.cmd == "requeue":
        print(f"pulled back {renderer(story, work, cfg).requeue()} queued tasks")
    elif args.cmd == "stop":
        r = renderer(story, work, cfg)
        print(f"paused ({r.pause_flag}); pulled back {r.stop()} unstarted tasks; running tasks finish")
    elif args.cmd == "resume":
        print("resumed" if renderer(story, work, cfg).resume() else "was not paused")
    elif args.cmd == "publish":
        pub = cfg.get("publish", {})
        dest = (cfg["_base"] / pub["dir"]).resolve() if pub.get("dir") else work / "publish"
        print("\n".join(render.publish(story, work, args.items.split(","), dest, pub.get("rclone"))))
    elif args.cmd == "qa":
        print("\n".join(qa.check(story, work, args.style)) or "no clips yet")
        if not args.no_grids:
            for sc in story.scenarios:
                video = work / args.style / f"{sc}.mp4"
                if video.exists():
                    print("grids:", " ".join(str(p) for p in qa.grids(video, work / args.style / "qa")))
    return 0
