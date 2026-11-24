---
name: storyboard-to-video
description: Produce consistent illustrated explainer stills and silent videos from a written story with this repo (story spec JSON → Codex image model stills → style repaints → MiniMax H3 first/last-frame clips on WanGP GPU nodes → stitched videos). Use for new scenarios, new styles, or re-renders. Not for single one-off images or text-to-video without approved stills.
---

# storyboard-to-video: agent procedure

You drive `python -m storyboard` from the control machine. The GPU nodes run `worker/qworker.sh`. Read the "How the consistency is achieved" section of README.md once before writing prompts. Those rules are the product.

## Inputs you need

- A story: who, where, what goes wrong. Start by copying `examples/starter/story.json` to a new directory and replacing its content; the spec owns every prompt and creative decision.
- `config.json` with reachable nodes. Run `python -m storyboard status` to confirm. If it fails, the task is blocked on access: report which node and what error. Do not invent hosts.
- Codex CLI signed in with `image_generation` enabled (`codex features list | grep image_generation`).

## Procedure

1. **Spec.** Write or extend the story spec. Set `export STORYBOARD_SPEC=... STORYBOARD_CONFIG=config.json`, then run `python -m storyboard check` until it prints OK.
   - `bible`: the room layout with every prop's fixed position, each character's exact look, the style, and "no text, no captions, no extra people or characters".
   - Each frame `action`: one moment, written as a *state*, not a motion. Name where every moving thing is (on the floor at the table's right edge…). Spell out object states, such as "the container is EMPTY: its inside is clean bare metal". Repeat facts that must persist ("the lid is back on the tin").
   - Frame `parent`: the previous story frame. New scenarios should branch from shared frames so shared segments are reused.
   - Each segment `path`: the continuous motion from still A to still B in one paragraph, in order, with the same position words as the actions. `frames` must be on the 17k+5 grid. Prefer 124–175; 243 is the practical maximum.
   - A style `look`: one sentence of rendering terms (lines, shading, grain, palette, light), plus a reference image in `ref`.
2. **Master stills.** Run `python -m storyboard stills --styles <master>`, then `python -m storyboard sheet <master>` and open the sheet image.
   - Check every frame against the bible and against its neighbours: same camera, props in place, characters on model, object states right, nobody teleporting.
   - Bad frame: rewrite its action, then `reject <master> <frame>` and run `stills` again. Rejecting a master frame cascades to its repaints and child frames.
3. **Style stills.** Generate one style's anchor first (`stills --styles <style> --only <anchor_frame>`) and compare it with the master and the reference.
   - If it is only a recolour, strengthen the `look` and redo the anchor.
   - Then run `stills --styles <style>` and review its sheet the same way. Zoom into state-critical props (container interiors, doors, lids) with a crop. Thumbnails hide them.
4. **Video.** Start the loop as a background job that survives your turn (`python -m storyboard run --every 300`, or tmux). Do not poll it in the foreground. Check back every 20–30 minutes and read `work/<story>/STATUS.txt` plus the tail of the loop's output.
5. **Review each finished video.** Run `python -m storyboard qa <style>`. It scores every clip against its two stills and writes 0.5 s grids per video to `work/<story>/<style>/qa/`. Investigate every FLAG (zoom, cut and invented content score above 2), then look through every grid anyway; `<scenario>_strip.png` (8 frames per segment) is only an overview and misses events shorter than a second, like a quick press.
   - If a moment looks wrong, extract it at full resolution with `ffmpeg -ss <sec> -i <video> -frames:v 1 x.png` and look.
   - A clip-only artifact (in the clip but in neither still) gets `redo <style> <from>__<to>`.
   - A still-level problem gets `reject` plus `stills`.
   - Keep a ledger row per video: seeds, verdict (KEEP / REDO / REJECT) and the reason.
6. **Publish only reviewed videos:** `python -m storyboard publish <style>/<scenario>,...`. This rebuilds `overview_<story>.png`, and also runs `rclone copy` if `config.publish.rclone` is set. Several stories can publish into one folder.
7. **Stopping.** Run `python -m storyboard stop`. It persists a pause flag that every pump obeys, including the one inside a running `run` loop, and it pulls back all unstarted tasks. Then confirm with `python -m storyboard status`: it shows a `PAUSED since …` line and no queued segments.
   - Tasks that are already running finish, and the loop still collects and stitches their clips.
   - To shut the loop down as well, end the `run` job, then run `stop` once more: that pulls back anything a cycle was queueing at the moment you stopped it.
   - To abort a running task immediately, kill that task's `gen.py` process on its node, and only that process.
   - `python -m storyboard resume` starts queueing again.

## Rules

- Never queue video work for stills you have not reviewed. A bad still wastes 10–25 GPU-minutes per segment that uses it.
- One locked camera inside a scenario. For a different camera angle, make a separate shot with its own `scene` text, or a separate production whose spec covers that shot.
- Keep the H3 settings from `storyboard/spec.py` unless you are measuring a change. `multi_prompts_gen_type: "FG"` and `config: "gguf_q4_k_m"` are required.
- H3 renders what its prompt names, and it completes actions the stills set up. Before you queue anything, check:
  - No blank or near-empty start frames: H3 ignores them and invents content.
  - The `h3.scene` must match the segment's stills. If a segment has a different camera, or someone is absent, give it its own `scene` and say what *is* there; negations ("no hands") prime the thing they forbid.
  - Diagram prompts name no characters and mention no text unless the stills show it, or H3 writes words.
  - A hand that must not touch something stays far from it in BOTH stills, and the path gives the hand a trajectory that never names the object ("lifts her hand straight up off the tabletop and holds it there"). "Ready to slam it", "reaching toward the switch" or "does not touch the switch" all produced touches. Add a `qa_watch` box around the object.
  - Describe a prop that must stay inert as what it is ("a plain painted object with no light in it"), in every scene text including a segment's own `scene`; H3 lit a dark indicator by itself. Never describe its state with light words ("dark", "unlit"): naming the light summoned a glow.
  - When H3 keeps inventing the same in-between state (a "stuck" door that swings open on every seed, a switch that flips itself), stop rerolling and pin the middle: add stills of the correct in-between moments and `inject` them into the segment. One keyframe holds only its own instant, so keep the gaps between pinned frames (endpoints included) at about 18 frames or less wherever the defect appeared. Where something moves through a gap, inject a mid-motion still rather than repeating an endpoint.
  - Diagram changes read best as a slide build ("fades in from transparent"). "An arrow grows" summons a drawing hand.
- Review every master video before the repaint styles render the same segments. They share the prompt, so a prompt bug repeats in every style. `render_order: "style"` gives you that window; if you find a bug, `stop`, fix it, `redo` the master segment and re-queue the others.
- Editing a `path` or `scene` while the loop runs: `run` re-reads the spec every cycle, but tasks already queued keep the prompt they were queued with. Run `stop` then `resume`: the unstarted tasks are pulled back and re-queued with the new prompt and the same seed. Use `redo` for segments already rendered with the old prompt.
- Never reuse a seed number for different stills. `reject`/`drop` keep each segment's seed counter for exactly this reason (an old render with the same name would be taken as the new one); don't hand-edit `state.json` to reset seeds.
- Deliverables are silent, with no burned-in text unless the user asks for it.
- Do not touch GPU clocks or power limits. Use bounded timeouts on every ssh/rsync call; the code already sets them.
- Never print or commit credentials. `config.json` and `work/` are gitignored.

## Before you say "done"

- `python -m storyboard status` shows every requested scenario × style as VIDEO.
- Each published video has a reviewed strip and a ledger verdict.
- You looked at real frames (sheet, strip, full-res grabs) and did not rely only on file existence.
- The publish dir or remote lists the files: `rclone lsf <remote>` if one is used.

## Failure guide

| symptom | cause | fix |
|---|---|---|
| every task fails within seconds | stills missing on the node (`No such file … .png` in `<root>/logs/<task>.log`) | `pump` syncs first and only queues on nodes holding both stills; check the node's `stills/<story>/<style>/` |
| WanGP splits one prompt into many requests | `multi_prompts_gen_type` not `FG` | use the defaults |
| node stops answering after hours | unified-memory exhaustion | one task per process (qworker) + `MIN_AVAIL_MB` watchdog; power-cycle if wedged |
| repaint changed an object's state | repaint prompt lacked the action | the code passes the action; make the action explicit and reject the frame |
| style collapses toward the master inside a clip | weak `look` text | next seed; then sharpen the `look` |
| Codex returns no file | transient | `codex_image` retries once; rerun `stills` (it only makes missing files) |
