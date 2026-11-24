"""Video: MiniMax H3 FL2VA clips between approved stills, rendered by WanGP queue workers on remote GPU nodes.

A segment is one clip from still A to still B. Segments shared by several scenarios render once per style.
`pump` is idempotent and safe to run on a timer: sync stills -> pull finished clips -> record failures -> queue
ready segments by priority (only on nodes that already hold both stills) -> stitch finished videos + review strips
-> write STATUS.txt and ledger.jsonl.

Remote layout under each node's `root` (worker/qworker.sh uses the same):
  stills/<story>/<style>/<frame>.png   queue/*.json   done/   failed/   logs/   out/<story>/<style>/<a>__<b>_s<seed>.mp4
"""
import json
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import media, qa
from .spec import Story


@dataclass
class Node:
    name: str
    ssh: str                 # user@host as `ssh` understands it (aliases from ~/.ssh/config work)
    root: str                # absolute remote work root, e.g. /home/me/sbq
    jump: str | None = None  # optional ProxyJump host

    def _ssh(self) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"] + (["-J", self.jump] if self.jump else [])

    def run(self, script: str, secs: int = 120, check: bool = True) -> str:
        r = subprocess.run(self._ssh() + [self.ssh, script], capture_output=True, text=True, timeout=secs)
        if check and r.returncode:
            raise RuntimeError(f"[{self.name}] rc={r.returncode}: {script[:120]} :: {r.stderr[-300:]}")
        return r.stdout

    def rsync(self, src: str, dst: str, *opts: str, secs: int = 600) -> None:
        r = subprocess.run(["rsync", "-az", *opts, "-e", " ".join(self._ssh()), src, dst],
                           capture_output=True, text=True, timeout=secs)
        if r.returncode:
            raise RuntimeError(f"[{self.name}] rsync {src} -> {dst} rc={r.returncode}: {r.stderr[-300:]}")

    def remote(self, path: str) -> str:
        return f"{self.ssh}:{path}"


def h3_prompt(look: str, scene: str, path: str, frames: int, fps: int, injected: list[int] = ()) -> str:
    """MiniMax's FL2VA prompt format (official h3-prompt-writing skill): alignment line, then the integrated
    description, soundscape and music sections. Sound is disabled because the deliverable is silent.
    `injected`: frame numbers of injected keyframes; WanGP numbers them Picture 3, 4, ... after the start and end."""
    end = f"{frames / fps:.2f}"
    mid = "".join(f"; Picture {i + 3} (from Shot 1) aligns with the {(at - 1) / fps:.2f}-second mark of the target video"
                  for i, at in enumerate(injected))
    held = (" In the middle the scene passes exactly through " +
            ", then ".join(f"the poses and object states of Picture {i + 3}" for i in range(len(injected))) + "."
            if injected else "")
    return (f"How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second "
            f"mark of the target video; Picture 2 (from Shot 1) aligns with the {end}-second mark of the target video"
            f"{mid}."
            f"\n\nintegrated_multimodal_description: [Shot 1] {look}. {scene} The camera holds a static shot. The scene "
            f"begins in the poses, positions and framing established by Picture 1. {path}{held} Toward the end "
            f"everything settles into the poses, object states and composition established by Picture 2.\n\n"
            f"overall_soundscape: N/A\n\nnon_diegetic_music: N/A")


class Render:
    def __init__(self, story: Story, work: Path, nodes: list[Node], max_seeds: int = 3):
        self.s, self.work, self.nodes, self.max_seeds = story, work, {n.name: n for n in nodes}, max_seeds
        self.state_file = work / "state.json"
        self.pause_flag = work / "PAUSED"   # written by `stop`, removed by `resume`; pump queues nothing while it exists

    # ------------------------------------------------------------------ state
    def load(self) -> dict:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def save(self, state: dict) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=1, sort_keys=True))

    def clip(self, style: str, a: str, b: str) -> Path:
        return self.work / style / "clips" / f"{a}__{b}.mp4"

    def still(self, style: str, fid: str) -> Path:
        return self.work / style / "stills" / f"{fid}.png"

    # ------------------------------------------------------------------ tasks
    def task(self, node: Node, style: str, a: str, b: str, seed: int, prio: int) -> dict:
        seg, h3 = self.s.segments[(a, b)], self.s.h3
        tid = f"{prio:03d}_{self.s.name}_{style}_{a}__{b}_s{seed}"
        stills = f"{node.root}/stills/{self.s.name}/{style}"
        scene = seg.get("scene", h3["scene"])   # a segment with its own camera (an insert) describes its own set
        # `frames` is the playback length. WanGP's H3 renders at least `min_frames` (107); a shorter segment is
        # rendered at that length and sped up to `frames` when stitching. Injection positions scale with it.
        n = max(seg["frames"], h3.get("min_frames", 0))
        inject = [dict(i, at=round(1 + (i["at"] - 1) * (n - 1) / (seg["frames"] - 1)))   # [{frame, at}]: exact keyframes
                  for i in seg.get("inject", [])]
        settings = dict(h3["settings"], prompt=h3_prompt(self.s.styles[style]["look"], scene, seg["path"],
                                                          n, h3["fps"], [i["at"] for i in inject]),
                        resolution=h3["resolution"], video_length=n, seed=seed,
                        image_start=f"{stills}/{a}.png", image_end=f"{stills}/{b}.png")
        if inject:
            settings.update(video_prompt_type="KFI", image_refs=[f"{stills}/{i['frame']}.png" for i in inject],
                            frames_positions=" ".join(str(i["at"]) for i in inject))
        return dict(id=tid, model_type=h3["model"], settings=settings, profile=h3.get("profile", 1),
                    out=f"{node.root}/out/{self.s.name}/{style}/{a}__{b}_s{seed}.mp4")

    # ------------------------------------------------------------------ transport
    def sync_stills(self) -> dict[str, set[str]]:
        """Mirror every style's stills to every node; return 'style/frame' names each node now holds."""
        held = {}
        for node in self.nodes.values():
            base = f"{node.root}/stills/{self.s.name}"
            dirs = " ".join(f"{base}/{st}" for st in self.s.styles)
            node.run(f"mkdir -p {dirs} {node.root}/queue {node.root}/out/{self.s.name}")
            for style in self.s.styles:
                src = self.work / style / "stills"
                if src.exists():
                    node.rsync(f"{src}/", node.remote(f"{base}/{style}/"), "--delete", "--include=*.png",
                               "--exclude=*")
            listing = node.run(f"cd {base} && ls */*.png 2>/dev/null; true")
            held[node.name] = {x[:-4] for x in listing.split() if x.endswith(".png")}
        return held

    def pull(self) -> None:
        dst = self.work / "_pull"
        dst.mkdir(parents=True, exist_ok=True)
        for node in self.nodes.values():
            node.rsync(node.remote(f"{node.root}/out/{self.s.name}/"), f"{dst}/", "--exclude=manifest.jsonl")

    def failed(self) -> set[str]:
        out = set()
        for node in self.nodes.values():
            out |= {Path(x).stem for x in node.run(f"ls {node.root}/failed 2>/dev/null; true").split()}
        return out

    def on_nodes(self) -> set[str]:
        """Task ids whose files sit in a node queue (waiting or running: the worker moves a file only when done)."""
        out = set()
        for node in self.nodes.values():
            out |= {Path(x).stem for x in node.run(f"ls {node.root}/queue 2>/dev/null; true").split()}
        return out

    def queue(self, node: Node, tasks: list[dict]) -> None:
        with tempfile.TemporaryDirectory() as d:
            for t in tasks:
                Path(d, f"{t['id']}.json").write_text(json.dumps(t, indent=1))
            node.rsync(f"{d}/", node.remote(f"{node.root}/queue/"))

    # ------------------------------------------------------------------ the loop step
    def pump(self) -> list[str]:
        state = self.load()
        held = self.sync_stills()
        alive = self.on_nodes()   # listed BEFORE the pull: a task finishing in between is caught by the pull
        self.pull()
        failed = self.failed()
        load = {n: 0 for n in self.nodes}
        lost = 0
        stale = []
        for key, st in list(state.items()):
            style, seg = key.split("/")
            got = self.work / "_pull" / style / f"{seg}_s{st['seed']}.mp4"
            if st["status"] == "queued" and got.exists():
                a, b = seg.split("__")
                sa, sb = self.still(style, a), self.still(style, b)
                err = qa.endpoint_error(got, sa, sb) if sa.exists() and sb.exists() else float("inf")
                if err > qa.STALE:
                    # rendered from other versions of the stills (a node still held a replaced file when the task
                    # ran): refuse it; the next pump renders the next seed against the current stills
                    st["status"] = "stale"
                    stale.append(f"{key} s{st['seed']} ({err:.1f})")
                    continue
                tripped = [f"{w['name']} {score:.0f}" for w in self.s.raw.get("qa_watch", [])
                           if w.get("enforce", True) and (not w.get("segments") or seg in w["segments"])
                           for score in [qa.watch(got, sa, sb, w["box"])[0]] if score > w["max"]]
                if tripped:
                    # a region that must not change did (a button touched, a stuck door opening): never accept the
                    # clip; the next pump renders the next seed
                    st["status"] = "watch"
                    stale.append(f"{key} s{st['seed']} ({', '.join(tripped)})")
                    continue
                dst = self.work / style / "clips" / f"{seg}.mp4"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(got, dst)
                st["status"] = "done"
            elif st["status"] == "queued" and st["tid"] in failed:
                st["status"] = "failed"
            elif st["status"] == "queued" and st["tid"] not in alive:
                # not on any node, not finished, not failed: pulled back by stop/requeue (maybe in another process),
                # or recorded by a racing pump. Mark it lost: it is queued again with the SAME seed unless paused.
                st["status"] = "lost"
                lost += 1
            if st["status"] == "queued" and st["node"] in load:
                load[st["node"]] += st["frames"]
        if self.pause_flag.exists():
            # paused: queue nothing, and pull back unstarted tasks (one may have slipped in from a pump cycle that
            # was already running when `stop` was issued); running tasks finish and are still collected + stitched
            pulled = self._unqueue(state)
            self.save(state)
            log = [f"PAUSED ({self.pause_flag}); no new tasks" + (f", pulled back {pulled}" if pulled else "")
                   + (f", {lost} lost" if lost else "")]
            log += [f"refused clips (replaced stills or a qa_watch region): {', '.join(stale)}"] if stale else []
            log += self.stitch_ready(state)
            self.write_status(state, load)
            return log
        new, planned = defaultdict(list), []
        for (style, a, b), prio in sorted(self.s.priorities().items(), key=lambda kv: kv[1]):
            key = f"{style}/{a}__{b}"
            if self.clip(style, a, b).exists():
                if key in state:
                    state[key]["status"] = "done"
                continue
            st = state.get(key)
            if st and st["status"] == "queued":
                continue   # a "done" entry whose clip is gone was redone (`redo` deletes the clip): next seed
            seed = (st["seed"] if st["status"] == "lost" else st["seed"] + 1) if st else 1
            need = {f"{style}/{a}", f"{style}/{b}"} | {f"{style}/{i['frame']}" for i in self.s.segments[(a, b)].get("inject", [])}
            nodes = [n for n in self.nodes if need <= held[n]]
            if seed > self.max_seeds or not nodes:
                continue
            name = min(nodes, key=lambda n: load[n])
            frames = self.s.segments[(a, b)]["frames"]
            load[name] += frames
            t = self.task(self.nodes[name], style, a, b, seed, prio)
            new[name].append(t)
            state[key] = dict(status="queued", seed=seed, node=name, tid=t["id"], frames=frames, ts=int(time.time()))
            planned.append(key)
        if new and self.pause_flag.exists():   # `stop` landed while this cycle was planning: queue nothing
            new.clear()
            for key in planned:
                state[key]["status"] = "lost"  # keeps its seed; queued after `resume`
        log = [f"{lost} tasks were no longer on any node; re-queued with their seed"] if lost else []
        log += [f"refused clips, next seed (replaced stills or a qa_watch region): {', '.join(stale)}"] if stale else []
        for name, tasks in new.items():
            self.queue(self.nodes[name], tasks)
            log.append(f"queued {len(tasks)} on {name}: {', '.join(t['id'] for t in tasks)}")
        self.save(state)
        log += self.stitch_ready(state)
        self.write_status(state, load)
        return log

    def stitch_ready(self, state: dict) -> list[str]:
        log = []
        for style in self.s.styles:
            for sc in self.s.scenarios:
                clips = [self.clip(style, a, b) for a, b in self.s.pairs(sc)]
                out = self.work / style / f"{sc}.mp4"
                if not all(c.exists() for c in clips):
                    continue
                if out.exists() and out.stat().st_mtime >= max(c.stat().st_mtime for c in clips):
                    continue
                media.stitch(clips, out, fps=self.s.h3["fps"],
                             lengths=[self.s.segments[p]["frames"] for p in self.s.pairs(sc)])
                media.strip(clips, self.work / style / f"{sc}_strip.png")
                seeds = {f"{a}__{b}": state.get(f"{style}/{a}__{b}", {}).get("seed") for a, b in self.s.pairs(sc)}
                with (self.work / "ledger.jsonl").open("a") as fh:
                    fh.write(json.dumps(dict(ts=int(time.time()), video=f"{style}/{sc}", seeds=seeds, out=str(out),
                                             strip=str(self.work / style / f"{sc}_strip.png"),
                                             verdict="pending-review")) + "\n")
                log.append(f"stitched {style}/{sc}")
        return log

    def write_status(self, state: dict, load: dict | None = None) -> str:
        lines = [time.strftime("%Y-%m-%d %H:%M:%S"), f"pending frames per node: {load}" if load else "",
                 f"PAUSED since {self.pause_flag.read_text().strip()} (run `resume` to queue again)"
                 if self.pause_flag.exists() else "", ""]
        for style in self.s.styles:
            for sc, seq in self.s.scenarios.items():
                n = sum(self.still(style, f).exists() for f in seq)
                segs = ["done" if self.clip(style, a, b).exists() else state.get(f"{style}/{a}__{b}", {}).get("status", "-")
                        for a, b in self.s.pairs(sc)]
                vid = "VIDEO" if (self.work / style / f"{sc}.mp4").exists() else ""
                lines.append(f"{style:8} {sc:12} stills {n}/{len(seq)}  segs {' '.join(segs):44} {vid}")
        text = "\n".join(lines) + "\n"
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "STATUS.txt").write_text(text)
        return text

    # ------------------------------------------------------------------ human decisions
    def redo(self, style: str, seg: str) -> None:
        """Reject a clip: the next pump queues the next seed for it."""
        self.clip(style, *seg.split("__")).unlink(missing_ok=True)
        state = self.load()
        a, b = seg.split("__")
        # clips imported by hand have no state entry: they count as seed 1, so the redo renders seed 2
        st = state.setdefault(f"{style}/{seg}", dict(seed=1, node=None, tid="", frames=self.s.segments[(a, b)]["frames"],
                                                      ts=int(time.time())))
        st["status"] = "failed"
        self.save(state)
        for sc in self.s.scenarios:
            if seg in [f"{a}__{b}" for a, b in self.s.pairs(sc)]:
                (self.work / style / f"{sc}.mp4").unlink(missing_ok=True)

    def drop(self, stills: list[str]) -> None:
        """After stills were rejected: delete clips/videos that used them and pull their queued tasks back."""
        state = self.load()
        for item in stills:
            style, fid = item.split("/")
            for a, b in self.s.segments:
                if fid in (a, b):
                    self.clip(style, a, b).unlink(missing_ok=True)
                    st = state.get(f"{style}/{a}__{b}")
                    if not st:
                        continue
                    if st["status"] == "queued" and st["node"] in self.nodes:
                        node = self.nodes[st["node"]]
                        node.run(f"rm -f {node.root}/queue/{st['tid']}.json", check=False)
                    # keep the seed counter: the next render gets seed+1, a new output name, so an old render made
                    # from the rejected still (still in out/ and _pull/) can never be taken for the new one
                    st["status"] = "dropped"
            for sc, seq in self.s.scenarios.items():
                if fid in seq:
                    (self.work / style / f"{sc}.mp4").unlink(missing_ok=True)
        self.save(state)

    def _unqueue(self, state: dict) -> int:
        """Delete this story's unstarted task files from every node queue (the node listing is the truth, so a file
        a racing pump wrote but never recorded is caught too) and mark them lost in `state`. Running tasks stay."""
        pulled, gone = 0, set()
        for node in self.nodes.values():
            running = node.run("pgrep -af gen.py; true")
            files = node.run(f"cd {node.root}/queue 2>/dev/null && ls *_{self.s.name}_*.json 2>/dev/null; true").split()
            drop = [f for f in files if f[:-5] not in running]
            if drop:
                node.run(f"cd {node.root}/queue && rm -f {' '.join(drop)}")
            pulled += len(drop)
            gone |= {f[:-5] for f in drop}
        for st in state.values():
            if st["status"] == "queued" and st["tid"] in gone:
                st["status"] = "lost"      # re-queued later with the same seed
        return pulled

    def requeue(self) -> int:
        """Pull every not-yet-started task off the node queues; the next pump re-queues it by current priority."""
        state = self.load()
        pulled = self._unqueue(state)
        self.save(state)
        return pulled

    def stop(self) -> int:
        """Pause the story: persist the pause flag (every later pump, including a running `run` loop, queues
        nothing until `resume`) and pull back every unstarted task. Running tasks finish and are still collected."""
        self.work.mkdir(parents=True, exist_ok=True)
        self.pause_flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        return self.requeue()

    def resume(self) -> bool:
        was = self.pause_flag.exists()
        self.pause_flag.unlink(missing_ok=True)
        return was


def publish(story: Story, work: Path, items: list[str], dest: Path, rclone: str | None) -> list[str]:
    """Copy reviewed videos to <dest>/<scenario>/<style>.mp4, rebuild the overview, optionally `rclone copy`."""
    log = []
    for item in items:
        style, sc = item.split("/")
        dst = dest / sc / f"{style}.mp4"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / style / f"{sc}.mp4", dst)
        log.append(f"published {item}")
    overview(story, work, dest)
    if rclone:
        r = subprocess.run(["rclone", "copy", str(dest), rclone], capture_output=True, text=True, timeout=1800)
        if r.returncode:
            raise RuntimeError(f"rclone copy failed: {r.stderr[-300:]}")
        log.append(f"synced {dest} -> {rclone}")
    return log


def overview(story: Story, work: Path, dest: Path) -> Path:
    """One row per published scenario, one column per style; cell = the scenario's key frame."""
    from PIL import Image
    w, h = 480, 270
    keys = story.raw.get("key_frames", {})
    rows = [sc for sc in story.scenarios if (dest / sc).is_dir()]
    img = Image.new("RGB", (w * len(story.styles), h * max(1, len(rows))), "white")
    for r, sc in enumerate(rows):
        key = keys.get(sc, story.scenarios[sc][-1])
        for c, style in enumerate(story.styles):
            p = work / style / "stills" / f"{key}.png"
            if (dest / sc / f"{style}.mp4").exists() and p.exists():
                img.paste(Image.open(p).convert("RGB").resize((w, h)), (c * w, r * h))
    out = dest / f"overview_{story.name}.png"   # per story, so several stories can publish into one folder
    img.save(out)
    return out
