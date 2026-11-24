#!/usr/bin/env python3
"""Run ONE WanGP task in this process, then exit (unified-memory GPUs such as GB10 only release memory on exit).

Usage: gen.py task.json      (run inside WanGP's venv; WANGP_ROOT defaults to ~/Wan2GP)
task.json: {"id": str, "model_type": str, "out": "/abs/path/<id>.mp4", "settings": {...WanGP overrides...},
            "profile": 1}
Writes <out> and appends a row to <out dir>/manifest.jsonl. Exit code 0 = ok, 1 = generation failed.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("WANGP_ROOT", Path.home() / "Wan2GP")).expanduser()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from shared.api import init  # noqa: E402  (WanGP's in-process API)

PATH_KEYS = ("image_start", "image_end", "image_refs", "image_guide", "video_guide", "audio_guide")


def main() -> int:
    task = json.loads(Path(sys.argv[1]).read_text())
    out = Path(task["out"]).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 0:
        print(f"[skip] {out} exists", flush=True)
        return 0
    # profile 1 keeps every model resident: fastest when the GPU has the memory (128 GB unified on GB10)
    session = init(root=ROOT, cli_args=["--attention", "sdpa", "--profile", str(task.get("profile", 1))])
    settings = session.get_default_settings(task["model_type"])
    settings.update(task["settings"])
    for key in PATH_KEYS:
        val = settings.get(key)
        if isinstance(val, str):
            settings[key] = str(Path(val).expanduser())
        elif isinstance(val, list):
            settings[key] = [str(Path(v).expanduser()) if isinstance(v, str) else v for v in val]
    t0 = time.time()
    result = session.submit_task(settings).result()
    row = {"id": task["id"], "model_type": task["model_type"], "wall_s": round(time.time() - t0, 1),
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    files = [Path(f) for f in (result.generated_files or [])]
    produced = [f for f in files if f.suffix.lower() == out.suffix.lower()] or files
    if result.success and produced:
        shutil.copy2(produced[0], out)
        row.update(status="ok", file=str(out))
    else:
        row.update(status="failed", errors=[e.message for e in (result.errors or [])][:3])
    with (out.parent / "manifest.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    print("[row]", json.dumps(row), flush=True)
    return 0 if row["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
