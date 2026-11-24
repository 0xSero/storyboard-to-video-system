"""Review aids for rendered video.

check(): scores every clip against its two endpoint stills. A frame of a clean H3 interpolation looks like one of the
two stills (or something in between); a zoom, a cut, an invented label or an extra person makes some frame far from
both. Score = max over frames of (distance to the nearer still) / (distance between the stills), on 192x108 greyscale.
On this repo's productions, clean clips scored 0.6-1.8 and every zoom/cut/invented-content failure 2.6-12, so the
thresholds below flag gross failures only. Small-motion clips (a head turn, a glow) inflate the ratio: treat FLAG
there as "look", and look at every video regardless.

grids(): one sheet per 12 frames at `fps` (default 2) for each stitched video, for the frame-by-frame look that the
8-frame review strips are too coarse for (strips missed button presses lasting under a second).
"""
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .spec import Story

W, H = 192, 108
FLAG, LOOK = 2.0, 1.3
STALE = 5.0   # mean grey-level error of a clip's first/last frame vs its stills; clean H3 clips measured 2.2-2.6


def endpoint_error(clip: Path, still_a: Path, still_b: Path) -> float:
    """How far the clip's first and last frames are from the stills it should start and end on. A clip rendered from
    other versions of the stills (a node still held the old file, or a still was replaced later) scores 8+."""
    f = _frames(clip, W, H).astype(np.float32)
    return float(max(np.abs(f[0] - _still(still_a)).mean(), np.abs(f[-1] - _still(still_b)).mean()))


def _frames(video: Path, w: int, h: int, fps: float | None = None, gray: bool = True) -> np.ndarray:
    vf = (f"fps={fps}," if fps else "") + f"scale={w}:{h}"
    fmt, ch = ("gray", 1) if gray else ("rgb24", 3)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", vf, "-f", "rawvideo", "-pix_fmt", fmt, "-"],
                         capture_output=True, timeout=300).stdout
    return np.frombuffer(raw, np.uint8).reshape((-1, h, w) if gray else (-1, h, w, ch))


def _still(p: Path) -> np.ndarray:
    return np.asarray(Image.open(p).convert("L").resize((W, H), Image.BILINEAR), np.float32)


def watch(clip: Path, still_a: Path, still_b: Path, box: list[float]) -> tuple[float, float]:
    """Mean RGB change inside `box` (fractions x0,y0,x1,y1) of the frame least like either still: (score, seconds).
    A spec lists boxes around things that must not be touched (a button) or must not move (a stuck door); a hand on
    the button scored 32-52 in our renders, clean clips 8-22."""
    w, h = 1344, 768
    x0, y0, x1, y1 = int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)
    f = _frames(clip, w, h, gray=False)[:, y0:y1, x0:x1].astype(np.float32)
    A = np.asarray(Image.open(still_a).convert("RGB").resize((w, h)), np.float32)[y0:y1, x0:x1]
    B = np.asarray(Image.open(still_b).convert("RGB").resize((w, h)), np.float32)[y0:y1, x0:x1]
    d = np.minimum(np.abs(f - A).mean(axis=(1, 2, 3)), np.abs(f - B).mean(axis=(1, 2, 3)))
    return float(d.max()), float(d.argmax()) / 24


def check(story: Story, work: Path, style: str) -> list[str]:
    out = []
    watches = story.raw.get("qa_watch", [])   # [{name, box, max, segments?}] — see README
    for a, b in story.segments:
        clip = work / style / "clips" / f"{a}__{b}.mp4"
        sa, sb = work / style / "stills" / f"{a}.png", work / style / "stills" / f"{b}.png"
        if not (clip.exists() and sa.exists() and sb.exists()):
            continue
        f = _frames(clip, W, H).astype(np.float32)
        A, B = _still(sa), _still(sb)
        span = max(float(np.abs(A - B).mean()), 2.0)
        near = np.minimum(np.abs(f - A).mean(axis=(1, 2)), np.abs(f - B).mean(axis=(1, 2))) / span
        worst = int(near.argmax())
        tag = "FLAG" if near.max() > FLAG else "LOOK" if near.max() > LOOK else "ok  "
        line = f"{tag} {style}/{a}__{b}: score {near.max():.2f} at {worst / story.h3['fps']:.1f}s"
        for wt in watches:
            if wt.get("segments") and f"{a}__{b}" not in wt["segments"]:
                continue
            score, t = watch(clip, sa, sb, wt["box"])
            if score > wt["max"]:
                line = "FLAG" + line[4:] + f"; {wt['name']} {score:.0f} at {t:.1f}s"
        out.append(line)
    return out


def grids(video: Path, out_dir: Path, fps: float = 2.0) -> list[Path]:
    w, h = 520, 293
    frames = _frames(video, w, h, fps, gray=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for g in range(0, len(frames), 12):
        sheet = Image.new("RGB", (3 * w, 4 * h), "black")
        for i, fr in enumerate(frames[g:g + 12]):
            im = Image.fromarray(fr)
            d = ImageDraw.Draw(im)
            d.rectangle([0, 0, 58, 16], fill="black")
            d.text((4, 3), f"{(g + i) / fps:5.1f}s", fill="yellow")
            sheet.paste(im, ((i % 3) * w, (i // 3) * h))
        p = out_dir / f"{video.stem}_{g // 12:02d}.jpg"
        sheet.save(p, quality=88)
        made.append(p)
    return made
