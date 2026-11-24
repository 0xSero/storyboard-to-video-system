"""Stills: Codex image generation (OpenAI image model behind `codex exec`), frame by frame.

Consistency rules that made this work:
  * master style: every frame is an EDIT of its parent frame, with the chain's first frame attached as the canonical
    look, and every prompt restates the same continuity bible;
  * other styles: every frame is a REPAINT of the master frame (same composition), anchored to the style's own
    repaint of the anchor frame, and the prompt restates what the frame shows so object states survive the repaint;
  * every attachment is announced with its role ("Attached image 2: ...").
"""
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .spec import Story

FIRST = "Create the first frame of an illustrated story.\n\n{roles}\n\n{bible}\n\nTHIS FRAME: {action}{text}"
EDIT = ("Edit attached image 1 (the previous frame of this story) into the next frame. Keep the camera, framing, room, "
        "furniture, light, every prop's position, the characters' faces, hair, clothes, design and size, and the "
        "painting style exactly the same; change only what this frame's action requires.\n\n{roles}\n\n{bible}\n\n"
        "THIS FRAME: {action}{text}")
REPAINT = (
    "Repaint attached image 1 in a different illustration style. Keep its content exactly: the same camera, framing "
    "and composition, room layout, furniture, every prop's position and state (including any liquid, spill, open "
    "door or broken pieces), the characters' poses, gestures, expressions and positions, and their faces, hair, "
    "clothes, design, proportions and size. Change only the rendering to this STYLE: {look}. Redraw every surface in "
    "that style rather than filtering the source: drop its brushwork, colour grading and light, so the result looks "
    "like a frame from the style reference, not a recoloured copy.\n\n{roles}\n\n{text_rule}{shows}")
NO_TEXT = "No text, no captions, no logos, no extra people or characters."
ONLY_TEXT = ("The only text or symbols in the image: {text}, kept exactly as in attached image 1 (same characters, "
             "place and size). No other text, captions or logos, no extra people or characters.")
ASK = ("Use your image generation tool to produce exactly one {aspect} image, then save it as {out} (PNG) and print its "
       "path. Do not create or modify any other file.\n\n{prompt}")


def codex_image(prompt: str, attach: list[tuple[Path, str]], out: Path, aspect: str = "16:9",
                dry_run: bool = False, timeout: int = 480, attempts: int = 2) -> bool:
    """One image through `codex exec` (needs codex-cli with the image_generation feature, ~100 s per image)."""
    task = ASK.format(aspect=aspect, out=out, prompt=prompt)
    if dry_run:
        files = "\n".join(f"  -i {p}" for p, _ in attach)
        print(f"--- {out}\n{files}\n{task}\n", flush=True)
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(out.parent)]
    for p, _ in attach:
        args += ["-i", str(p)]
    args.append("-")
    log = out.parent / "codex.log"
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            r = subprocess.run(args, input=task, text=True, capture_output=True, timeout=timeout)
            tail = (r.stdout + r.stderr)[-400:]
        except subprocess.TimeoutExpired:
            tail = "timeout"
        ok = out.exists() and out.stat().st_size > 10_000
        with log.open("a") as fh:
            fh.write(f"{out.name} attempt {attempt} ok={ok} {time.time() - t0:.0f}s\n{tail}\n---\n")
        if ok:
            return True
    return False


def roles(attach: list[tuple[Path, str]]) -> str:
    return "\n".join(f"Attached image {i + 1}: {role}" for i, (_, role) in enumerate(attach))


class Stills:
    """Makes the stills of a story under <work>/<style>/stills/<frame>.png and assets under <work>/assets/."""

    def __init__(self, story: Story, work: Path, dry_run: bool = False):
        self.s, self.work, self.dry = story, work, dry_run
        self._locks: dict = defaultdict(threading.Lock)

    def still(self, style: str, fid: str) -> Path:
        return self.work / style / "stills" / f"{fid}.png"

    def asset(self, name: str) -> Path:
        return self.work / "assets" / f"{name}.png"

    def ref(self, path: str) -> Path:
        """'@assets/<name>' -> generated asset, '@<style>/<frame>' -> generated still, else a file in the spec dir."""
        if not path.startswith("@"):
            return self.s.resolve(path)
        head, name = path[1:].split("/", 1)
        if head == "assets":
            if not self.make_asset(name):
                raise RuntimeError(f"asset {name} unavailable")
            return self.asset(name)
        if not self.ensure(head, name):
            raise RuntimeError(f"still {head}/{name} unavailable")
        return self.still(head, name)

    def _import(self, style: str, fid: str, out: Path) -> bool:
        seed = self.s.styles[style].get("seed_dir")
        if not seed:
            return False
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            src = self.s.resolve(seed) / f"{fid}{ext}"
            if src.exists():
                from PIL import Image
                out.parent.mkdir(parents=True, exist_ok=True)
                Image.open(src).convert("RGB").save(out)
                return True
        return False

    def plan(self, style: str, fid: str) -> tuple[str, list[tuple[Path, str]]]:
        """The Codex prompt and role-labelled attachments for one still (dependencies must already exist)."""
        s, f = self.s, self.s.frames[fid]
        extra = [(self.ref(r["path"]), r["role"]) for r in f.get("refs", [])]
        text = (f"\n\nTEXT: the only text or symbols in this frame are {f['text']}; render them crisply and exactly, "
                f"and add no other text.") if f.get("text") else ""
        if style == s.master:
            parent = f.get("parent")
            if not parent:
                return FIRST.format(roles=roles(extra), bible=s.bible, action=f["action"], text=text), extra
            root = s.root_of(fid)
            attach = [(self.still(style, parent), "the previous frame of this story; edit it into the next frame")]
            if root != parent:
                attach.append((self.still(style, root), "frame 1 of this story, the canonical look of the room and "
                                                         "characters"))
            attach += extra
            return EDIT.format(roles=roles(attach), bible=s.bible, action=f["action"], text=text), attach
        st = s.styles[style]
        attach = [(self.still(s.master, fid), "the frame to repaint (content source)")]
        if fid != s.anchor:
            attach.append((self.still(style, s.anchor), "this style's canonical frame: match its palette, light, "
                                                         "texture and the way it renders the characters and the room"))
        if st.get("ref"):
            attach.append((s.resolve(st["ref"]), "the style reference frame whose look (grain, line work, palette, "
                                                 "light, the way faces and objects are rendered) to match"))
        shows = f"\n\nWhat this frame shows (keep every detail of it): {f['action']}"
        rule = ONLY_TEXT.format(text=f["text"]) if f.get("text") else NO_TEXT
        return REPAINT.format(look=st["look"], roles=roles(attach), text_rule=rule, shows=shows), attach

    def ensure(self, style: str, fid: str) -> bool:
        """Make one still and, first, everything it depends on. Thread-safe; dependencies form a DAG."""
        out = self.still(style, fid)
        with self._locks[("still", style, fid)]:
            if out.exists():
                return True
            if self._import(style, fid, out):
                return True
            s, f = self.s, self.s.frames[fid]
            if style == s.master:
                deps = [(style, f["parent"]), (style, s.root_of(fid))] if f.get("parent") else []
            else:
                deps = [(s.master, fid)] + ([(style, s.anchor)] if fid != s.anchor else [])
            if not all(self.ensure(*d) for d in deps):
                return False
            prompt, attach = self.plan(style, fid)
            return codex_image(prompt, attach, out, s.aspect, self.dry)

    def make_asset(self, name: str) -> bool:
        out = self.asset(name)
        with self._locks[("asset", name)]:
            if out.exists():
                return True
            a = self.s.assets[name]
            attach = [(self.ref(r["path"]), r["role"]) for r in a.get("refs", [])]
            return codex_image(f"{roles(attach)}\n\n{a['prompt']}", attach, out, a.get("aspect", self.s.aspect),
                               self.dry)

    def run(self, styles: list[str], only: list[str] | None = None, workers: int = 6) -> int:
        jobs, seen = [], set()
        injected = [i["frame"] for seg in self.s.segments.values() for i in seg.get("inject", [])]
        for style in styles:
            for seq in list(self.s.scenarios.values()) + [injected]:
                for fid in seq:
                    if (only is None or fid in only) and (style, fid) not in seen:
                        seen.add((style, fid))
                        jobs.append((style, fid))
        failed = 0
        with ThreadPoolExecutor(1 if self.dry else workers) as ex:
            for (style, fid), ok in zip(jobs, ex.map(lambda j: self.ensure(*j), jobs)):
                failed += not ok
                if not self.dry:
                    print(f"{style}/{fid}: {'ok' if ok else 'FAILED'}", flush=True)
        return failed

    def reject(self, style: str, fid: str) -> list[str]:
        """Delete a still; master rejections cascade to every style's repaint and to child frames."""
        styles = list(self.s.styles) if style == self.s.master else [style]
        frames = {fid}
        if style == self.s.master:
            grew = True
            while grew:
                kids = {k for k, f in self.s.frames.items() if f.get("parent") in frames}
                grew = not kids <= frames
                frames |= kids
        gone = []
        for st in styles:
            for f in frames:
                p = self.still(st, f)
                if p.exists():
                    p.unlink()
                    gone.append(f"{st}/{f}")
        return gone


def sheet(story: Story, work: Path, style: str) -> Path:
    """Contact sheet: one row per scenario, one column per frame."""
    from PIL import Image
    w, h = 384, 216
    seqs = list(story.scenarios.items())
    cols = max(len(seq) for _, seq in seqs)
    img = Image.new("RGB", (w * cols, h * len(seqs)), "white")
    for r, (_, seq) in enumerate(seqs):
        for c, fid in enumerate(seq):
            p = work / style / "stills" / f"{fid}.png"
            if p.exists():
                img.paste(Image.open(p).convert("RGB").resize((w, h)), (c * w, r * h))
    out = work / style / "sheet.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
