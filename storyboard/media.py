"""ffmpeg helpers: stitch FL2VA clips into one silent video, and review strips."""
import subprocess
import tempfile
from pathlib import Path


def stitch(clips: list[Path], out: Path, size: str = "1920:1080", fps: int = 24, lengths: list[int] | None = None) -> None:
    """Concatenate clips that share boundary stills: each later clip's first frame duplicates the previous clip's
    last frame (both are the same approved still), so it is dropped. Audio is discarded (H3 also generates sound).
    `lengths`: playback frames per clip; a clip rendered longer (H3's 107-frame minimum) is sped up to fit."""
    inputs, chains = [], []
    for i, clip in enumerate(clips):
        inputs += ["-i", str(clip)]
        trim = "trim=start_frame=1,setpts=PTS-STARTPTS," if i else ""
        have = frame_count(clip)
        speed = f"setpts={lengths[i] / have:.6f}*PTS," if lengths and lengths[i] < have else ""
        chains.append(f"[{i}:v]{trim}{speed}scale={size}:flags=lanczos,fps={fps},format=yuv420p[v{i}]")
    n = len(clips)
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-an",
                    "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-movflags", "+faststart", str(tmp)],
                   check=True)
    tmp.rename(out)


def frame_count(clip: Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=nb_frames",
                        "-of", "csv=p=0", str(clip)], capture_output=True, text=True)
    return int(r.stdout.strip() or 120)


def strip(clips: list[Path], out: Path) -> None:
    """Review strip: one row of 8 evenly spaced frames per clip."""
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i, clip in enumerate(clips):
            row = Path(d, f"r{i}.png")
            step = max(1, frame_count(clip) // 8)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vf",
                            f"select='not(mod(n\\,{step}))',scale=320:-2,tile=8x1", "-frames:v", "1", str(row)],
                           check=True)
            rows.append(row)
        args = sum((["-i", str(r)] for r in rows), [])
        stack = "".join(f"[{i}:v]" for i in range(len(rows))) + f"vstack=inputs={len(rows)}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex",
                        stack if len(rows) > 1 else "null", str(out)], check=True)


def grab(clip: Path, seconds: float, out: Path) -> None:
    """One full-resolution frame, for checking a moment the strip hints at."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(seconds), "-i", str(clip), "-frames:v", "1", str(out)],
                   check=True)
