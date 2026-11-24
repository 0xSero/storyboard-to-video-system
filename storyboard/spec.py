"""Story spec: one JSON file holds every prompt and every creative decision of a production.

Paths inside the spec are relative to the spec file. See README.md ("The story spec") for the schema and
examples/starter/story.json for a minimal production.
"""
import json
from pathlib import Path

TURBO_LORA = ("https://huggingface.co/DeepBeepMeep/MiniMax-H3/resolve/main/loras/"
              "minimax_h3_lightx2v_fl2v_turbo_8step_alpha8_v1.0_bf16.safetensors")

# MiniMax H3 FL2VA through WanGP. `config: gguf_q4_k_m` selects the Q4_K_M GGUF text encoder; `FG` keeps the whole
# prompt as ONE generation (any other multi-prompt mode splits the H3 prompt into several requests).
H3_DEFAULTS = {
    "model": "minimax_h3_fl2va_pruned",
    "resolution": "1344x768",
    "fps": 24,
    "frame_grid": [17, 5],           # WanGP H3 lengths are 17k+5 frames: 107, 124, 141, 158, 175, ... 243
    "settings": {
        "image_prompt_type": "SE",   # start + end image
        "multi_prompts_gen_type": "FG",
        "config": "gguf_q4_k_m",
        "activated_loras": [TURBO_LORA],
        "loras_multipliers": "1.0",
        "num_inference_steps": 8,
        "guidance_scale": 1,
        "flow_shift": 12,
    },
}


class SpecError(ValueError):
    pass


class Story:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.dir = self.path.parent
        d = json.loads(self.path.read_text())
        self.raw = d
        self.name: str = d["name"]
        self.bible: str = d["bible"]
        self.aspect: str = d.get("aspect", "16:9")
        self.master: str = d["master_style"]
        self.anchor: str = d["anchor_frame"]
        self.styles: dict = d["styles"]
        self.frames: dict = d["frames"]
        self.scenarios: dict = d["scenarios"]
        self.segments: dict = {(s["from"], s["to"]): s for s in d["segments"]}
        self.assets: dict = d.get("assets", {})
        self.render_order: str = d.get("render_order", "scenario")
        h3 = d.get("h3", {})
        self.h3 = {**H3_DEFAULTS, **{k: v for k, v in h3.items() if k != "settings"}}
        self.h3["settings"] = {**H3_DEFAULTS["settings"], **h3.get("settings", {})}
        self.h3.setdefault("scene", "")
        self.validate()

    def resolve(self, rel: str | None) -> Path | None:
        return None if rel is None else (self.dir / rel).resolve()

    def root_of(self, fid: str) -> str:
        """First frame of the edit chain that `fid` belongs to."""
        seen = set()
        while self.frames[fid].get("parent"):
            if fid in seen:
                raise SpecError(f"parent cycle at {fid}")
            seen.add(fid)
            fid = self.frames[fid]["parent"]
        return fid

    def pairs(self, scenario: str) -> list[tuple[str, str]]:
        seq = self.scenarios[scenario]
        return list(zip(seq, seq[1:]))

    def priorities(self) -> dict[tuple[str, str, str], int]:
        """(style, a, b) -> render priority. render_order "scenario" (default): each scenario in every style before
        the next scenario; "style": every scenario in the first style (put the master first) before the next style."""
        prio: dict = {}
        if self.render_order == "style":
            order = [(st, sc) for st in self.styles for sc in self.scenarios]
        else:
            order = [(st, sc) for sc in self.scenarios for st in self.styles]
        for i, (style, sc) in enumerate(order):
            for a, b in self.pairs(sc):
                prio.setdefault((style, a, b), i)
        return prio

    def validate(self) -> None:
        errs = []
        if self.render_order not in ("scenario", "style"):
            errs.append(f"render_order {self.render_order!r} must be 'scenario' or 'style'")
        if self.master not in self.styles:
            errs.append(f"master_style {self.master!r} is not in styles")
        if self.anchor not in self.frames:
            errs.append(f"anchor_frame {self.anchor!r} is not in frames")
        for fid, f in self.frames.items():
            if "action" not in f:
                errs.append(f"frame {fid}: missing action")
            if f.get("parent") and f["parent"] not in self.frames:
                errs.append(f"frame {fid}: unknown parent {f['parent']!r}")
            for r in f.get("refs", []):
                if not r["path"].startswith("@") and not self.resolve(r["path"]).exists():
                    errs.append(f"frame {fid}: ref not found {r['path']}")
        for sid, st in self.styles.items():
            if "look" not in st:
                errs.append(f"style {sid}: missing look")
            if sid != self.master and st.get("ref") and not self.resolve(st["ref"]).exists():
                errs.append(f"style {sid}: ref not found {st['ref']}")
        k, c = self.h3["frame_grid"]
        for sc, seq in self.scenarios.items():
            for fid in seq:
                if fid not in self.frames:
                    errs.append(f"scenario {sc}: unknown frame {fid}")
            for a, b in zip(seq, seq[1:]):
                seg = self.segments.get((a, b))
                if seg is None:
                    errs.append(f"scenario {sc}: no segment {a} -> {b}")
                elif (seg["frames"] - c) % k:
                    errs.append(f"segment {a}->{b}: {seg['frames']} frames is not on the {k}k+{c} grid")
        for (a, b), seg in self.segments.items():
            for inj in seg.get("inject", []):
                if inj.get("frame") not in self.frames:
                    errs.append(f"segment {a}->{b}: inject frame {inj.get('frame')!r} is not in frames")
                if not isinstance(inj.get("at"), int) or not 1 < inj["at"] < seg["frames"]:
                    errs.append(f"segment {a}->{b}: inject 'at' must be a frame number strictly inside 1..{seg['frames']}")
        if errs:
            raise SpecError(f"{self.path}:\n  " + "\n  ".join(errs))
