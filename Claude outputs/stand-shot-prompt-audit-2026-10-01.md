# Earring on Stand Shot: prompt architecture audit (read-only)

Audited 2026-10-01 against `main` @ `60f7553`, plus the working tree. Uncommitted edits from parallel tasks touch `meta_whatsapp_service.py`, `config.py`, the middleware and some routes. None of the `earring_*_prompt.py` builders have uncommitted changes. No source files were changed. Verbatim prompt text for every live shot is in `claude/live-prompts-2026-09-28.md`, and this audit doesn't repeat it.

---

## 1. How shot styles are built and stored

### 1.1 One module per shot, one builder per module

| Shot | Module (`backend/app/services/`) | Builder | Parameters | Where it is used |
|---|---|---|---|---|
| ₹50 Clean Studio (white) | `ecommerce_shot_prompt.py` | `build_ecommerce_shot_prompt()` | none | WhatsApp white-background worker, 1:1 |
| P1 Clean E-Commerce | `earring_ecommerce_prompt.py` | `build_earring_ecommerce_prompt(earring_type)` → v1 or v2 | `earring_type`; `EARRING_PROMPT_VERSION` setting | Pack slot 1, 4:5 |
| P2 Close-up on Ear | `earring_close_up_ears_prompt.py` | `build_close_up_ears_prompt()` | none | Pack slot 2 |
| P3 Scale Reference | `earring_scale_reference_prompt.py` | `build_scale_reference_prompt(earring_type)` | `earring_type` | Pack slot 3 |
| P4 Professional Studio | `earring_professional_shot_prompt.py` | `build_professional_shot_prompt(earring_type, environment)` | `earring_type`, `environment` ∈ minimalist/organic/luxury | Pack slot 4 (minimalist); also `POST /api/earring-professional-shot/prompt` |
| P5 Lifestyle | `earring_complementary_shot_prompt.py` | `build_complementary_shot_prompt(earring_type)` | `earring_type` | Pack slot 5 |
| P6 UGC | `earring_ugc_style_prompt.py` | `build_ugc_style_prompt(earring_type)` | `earring_type` | Pack slot 6 |
| P7 Macro | `earring_macro_shot_prompt.py` | `build_macro_shot_prompt(earring_type)` | `earring_type` | Not in the pack; only `POST /api/earring-macro-shot/prompt` |

**Every builder follows the same pattern.** It builds `parts: list[str]`, appends module constants and inline blocks in a fixed order, and returns `"\n\n".join(parts)`. The text is fixed: there is no Jinja, no `str.format` and no template files. Each module's docstring includes an "Architecture" diagram of the layering.

### 1.2 Shared constant library

- **`earring_ecommerce_prompt.py` is the de-facto shared library**, which the modules call "Prompt 1". It exports `REFERENCE_PRIORITY_MARKER`, `ANTI_SYMMETRY_INSTRUCTION`, `COLOUR_LOCK_INSTRUCTION`, `MATERIAL_FIDELITY_INSTRUCTION`, `EARRING_TYPE_PRESERVATION` (a dict), `GENERIC_EARRING_PRESERVATION`, `INPUT_CLEANUP_INSTRUCTION`, `ANGLE_PRESERVATION_INSTRUCTION` and `PURE_WHITE_BACKGROUND_INSTRUCTION`.
- **`earring_scale_reference_prompt.py`** exports `NEGATIVE_SPACE_INSTRUCTION` and `BEAD_CLUSTER_PRESERVATION_INSTRUCTION`, which P2 and P6 import.
- **`earring_complementary_shot_prompt.py` keeps its own copies** of `NEGATIVE_SPACE_INSTRUCTION` and `BEAD_CLUSTER_PRESERVATION_INSTRUCTION` (line-wrapped differently). They have drifted, which is existing debt.
- **`backend/app/ai/product_fidelity.py`** holds `REFERENCE_PRIORITY_BLOCK`, `PRODUCT_PRESERVATION_CONSTRAINTS` and `REFERENCE_IMAGE_ANCHOR`.

### 1.3 Registration (where a new shot would be wired in)

- **The pack registry is in `meta_whatsapp_service.py`.** `CATALOG_PACK_STYLES` (line 35) is a list of `(title, prompt_type)`. The `style_prompt_builders` dict (around line 1146) is built inside `process_whatsapp_catalog_pack()` with local imports. Each builder is called as `builder()`, with **no arguments**.
- **Standalone routes** live in `api/routes/earring_professional_shot.py` and `earring_macro_shot.py`, and are registered in `main.py`. Each returns `{success, prompt, earring_type, …}`. The frontend then posts the prompt and reference image to `/api/generate-image`.
- **Tests that pin the pack:** `tests/test_ecom_pack1_restore.py` asserts `CATALOG_PACK_STYLES == EXPECTED` (exactly 6), `"all 6 styles"` in the acknowledgement copy, `gen.await_count == 6`, a price of 500, and "Delivered 5/6" partial logic.

### 1.4 How dynamic inputs reach the prompt

**For the live shots, metal, gemstones, ambient setting and lighting presets are not injected at all. This is deliberate.** The reference image is the only source of product truth, and text only describes presentation. The only dynamic inputs that exist:

| Input | Mechanism | Live value |
|---|---|---|
| `earring_type` | `EARRING_TYPE_PRESERVATION.get(t, GENERIC_EARRING_PRESERVATION)` with keys `Hoop`, `Stud`, `Dangle` | Always `None` in production, so the generic block is used. Workers call `builder()`. |
| `environment` (P4) | `VALID_ENVIRONMENTS`, `ENVIRONMENT_LABELS`, plus dispatch dicts `ENVIRONMENT_INSTRUCTIONS[env]()` and `LIGHTING_INSTRUCTIONS[env]()`. An unknown value falls back to `"minimalist"`. | `minimalist` |
| Prompt version (P1) | `PROMPT_VERSIONS = {"v1","v2"}`, chosen by `settings.EARRING_PROMPT_VERSION`; anything unknown falls back to v1 | v1 |
| Aspect ratio | `context={"aspect_ratio": …}` to the provider (outside the prompt). Gemini falls back to 4:5 for unsupported ratios. | 4:5 for the pack, 1:1 for white-bg |
| Metal colour | **Reference-bound wording**, not a variable. P7 `METAL_AFFIRMATION_INSTRUCTION`: "Match the metal colour … of the reference image EXACTLY … render yellow gold as yellow gold, …" | n/a |

**Two older systems do inject facts, but neither feeds the WhatsApp shots:**

- **`prompt_generation_service.py`** (the `/prompts` route and `prompt_tasks`) uses f-strings over DB analysis: `material`, `gold_purity`, `gemstones[:3]`, `style`, `era` → `productIdentity`, `materialProperties`.
- **`prompt_fusion_engine.py` (PFIE)** is off by default (`PFIE_ENABLED=False`). It normalises facts into the keys `metal`, `stones`, `stone_placement`, `shape`, `finish`, `size_category`, `wear_position` and so on.

### 1.5 What wraps the builder output at generation time

1. **`ImageGenerationManager.generate_image()`** appends `REFERENCE_PRIORITY_BLOCK` only if the prompt lacks `"REFERENCE IMAGE PRIORITY"`. Every builder embeds the marker, so it is never appended. It appends the marketplace block (`ai/marketplaces/amazon_india.py`) only if `marketplace=` is passed, and the workers don't pass it.
2. **Gemini** receives `[REFERENCE_IMAGE_ANCHOR, <photo>, <prompt>]`.
3. **The OpenAI fallback** gets `OPENAI_IDENTITY_ANCHOR` prepended.

---

## 2. Prompt anatomy

### 2.1 Camera

| Shot | Lens / aperture | Angle / framing |
|---|---|---|
| Clean Studio / P1 | None stated | Front-facing "as worn", centred, about 80–85% fill; P1 also has `ANGLE_PRESERVATION` (keep the reference orientation) |
| P2 Ear | "85–100mm macro … approximately f/4" | Partial side profile; complete earring in frame |
| P4 Professional | "Approximately 100mm macro … f/4", with the disclaimer *"visual photographic guidance, not guaranteed physical camera parameters"* | Not stated |
| P5 Lifestyle | None | "Elevated three-quarter … approximately 45-degree", avoid extreme perspective |
| P6 UGC | "High-end smartphone" look, natural depth of field | Not stated |
| P7 Macro | "100mm macro lens at f/2.8, extreme shallow depth of field" | Tight crop of one focal section filling 85% of the frame |
| Legacy service | "50mm f/2.8 macro" | "Hero angle at 45 degrees" |

**Convention:** a 100mm macro equivalent at f/4 is the house default for studio product shots. Every shot that shows the whole piece adds the caveat that depth of field must never blur any jewellery component.

### 2.2 Lighting

- **Clean Studio:** "bright professional studio softbox lighting (key + fill), even and neutral white balance; crisp specular highlights on metal and natural sparkle in stones without blown-out areas".
- **P4 minimalist:** balanced highlights, clean metal-edge definition, controlled gemstone brilliance without blown highlights, subtle environmental reflections on polished metal. Avoid dramatic directional light, high contrast, warm/cool tint, excessive sparkle, flat light and CGI reflections.
- **P4 gemstones:** realistic facet visibility, controlled brilliance, "appropriate dispersion where naturally visible". Avoid glowing stones and blown highlights.
- **P5:** soft directional light; "no exaggerated glow, artificial sparkle, or fantasy lighting".
- **The invariant in every block:** "Lighting may change illumination ONLY … must NEVER change the perceived underlying product material or colour" (`COLOUR_LOCK`).
- **Gaps:**
  - **Caustics:** no shot mentions caustics. Brilliance control stands in for them.
  - **Rim highlights:** not used anywhere. The closest is "clean metal-edge definition".
  - **Reflection control:** only "no reflections of photographers, equipment" (P1) and "no unrealistic CGI reflections" (P4).

### 2.3 Staging syntax

Every section uses the same pattern:

```
SECTION NAME (NON-NEGOTIABLE):
<1–3 lines of positive direction>
Allowed examples: / • bullet list
DO NOT use: / Do NOT generate: / • bullet list
```

- **Surfaces in use:**
  - P4 minimalist: "clean, premium, minimal surface", "mount/stand environment".
  - P4 organic: slate, travertine, concrete.
  - P4 luxury: champagne or ivory silk.
  - P5: travertine, matte stone, plaster, geometric podium, ribbed surface.
  - P6: vanity tray, open box, wooden desk, linen.
- **Backgrounds:**
  - White-bg, P1 and P3: `#FFFFFF`, spelled out three ways.
  - P2: warm grey or beige out-of-focus.
  - P4/P5: muted neutrals (beige, warm grey, cream, stone).
- **Grounding** has its own block in P4, P5 and P6: contact shadow, ambient occlusion, "no floating jewellery".

---

## 3. Negative constraints and guardrails

**There is no negative-prompt parameter or negative embedding anywhere.** A search of `backend/` and `ai-engine/` for `negative_prompt` returns nothing. Neither Gemini image generation nor OpenAI `images.edit` takes one. All negatives are inline natural-language blocks:

| Block | Source | What it guards against |
|---|---|---|
| `ANTI_SYMMETRY_INSTRUCTION` | ecommerce:111 | Mirroring or symmetrising the product, normalising geometry, beautifying |
| `COLOUR_LOCK_INSTRUCTION` + `MATERIAL_FIDELITY_INSTRUCTION` | ecommerce:200 / :88 | Silver↔gold drift, oversaturation, white background read as white metal |
| `METAL_AFFIRMATION_INSTRUCTION` | macro:67 | Affirmative metal lock. Lesson recorded in the module: *listing "silver/white-gold" as a preserve target primed silver hallucination* |
| `NEGATIVE_SPACE_INSTRUCTION` | scale:200 | Filling openwork, hollow frames or the gaps between drops |
| `BEAD_CLUSTER_PRESERVATION_INSTRUCTION` | scale:227 | Fused, melted, missing or invented beads and drops |
| `INPUT_CLEANUP` + `ANTI_RECONSTRUCTION` | ecommerce:141 | Removing hooks along with the display card; inventing occluded geometry |
| `ANGLE_PRESERVATION_INSTRUCTION` | ecommerce:169 | Rotating the product "to look prettier" |
| `NEGATIVE_CONSTRAINTS` | professional:242 | Altered prong or stone count, distorted geometry, floating product, missing contact shadows, CGI plastic look, duplicates |
| `NEGATIVE_FAILURE_PREVENTION` | scale:254 | Pins piercing skin, deformed or stretched hoops, extra jewellery |
| `STRICTLY_FORBIDDEN` | complementary:304, inline in UGC | "No floating pieces", "no impossible physics", "no warped prongs", "no unrealistic reflections" |
| `_MACRO_GLOBAL_NEGATIVES` | macro:109 | A deliberately short list ("negative lists do not scale") |
| Final verification checklists | P3, P4, P6 | 10-question self-check. **P7 removed it as "non-functional for image models and token-diluting".** |

**Your requested negatives against current coverage:**

| Requested | Current coverage |
|---|---|
| Asymmetry | Covered, but in the opposite direction: the system forbids *symmetrising*. See the warning in §4.4. |
| Floating parts | Covered |
| Warped metal | Covered ("warped prongs", "deformed geometry", "stretched jewellery") |
| Extra prongs | Covered ("altered prong count") |
| Distorted reflections | Partial: only "unrealistic CGI reflections" |

**Missing for a stand shot:**

- the earring merging into the stand
- the hook fused to the bar
- an invented hook back behind the bar
- the stand's colour tinting the metal
- the pair hung at mismatched heights
- earrings defying gravity (tilted drops)

---

## 4. Integration blueprint: "Earring on Stand Shot"

### 4.1 Collision map (what the new file must not import or contradict)

| Existing text | Location | Conflict | Rule for the new file |
|---|---|---|---|
| "no mannequin, **no stand**, no box" | `ecommerce_shot_prompt.py:64` | Forbids stands outright | Don't import any `ECOMMERCE_SHOT_*` constant |
| "No distracting props, **stands, acrylic holders**" | `ECOMMERCE_PRESENTATION_INSTRUCTION` (:191); v2 `V2_PRESENTATION` (:405) | Same | Don't import |
| "zero **podium, zero pedestal**" | `PURE_WHITE_BACKGROUND_INSTRUCTION` | Same | Don't import |
| "Do NOT generate: … **minimalist studio stands**" | P4 organic/luxury environments | Only in those archetypes | Stand shot is its own module, so there's no clash. Don't add it as a 4th P4 archetype, or the P4 "rests on surface" grounding block would contradict hanging. |
| P4 minimalist "mount/stand environment … rests naturally on a … surface" | professional:77 | Already ambiguous, so the pack's P4 may sometimes render a stand | Make the new shot clearly different: hanging on a bar, not resting. Optionally tighten P4 later (out of scope). |
| "Do not turn a single piece into a pair" | `REFERENCE_PRIORITY_BLOCK`, Clean Studio STEP 1 | "Bilateral hanging symmetry" assumes a pair | Branch on single vs pair (see §4.4) |
| `ANTI_SYMMETRY_INSTRUCTION` | ecommerce:111 | "Bilateral symmetry" could be read as "make the earrings symmetric" | Define symmetry as *placement only* (see §4.4) |

**Safe to import unchanged:**

- `REFERENCE_PRIORITY_MARKER`
- `ANTI_SYMMETRY_INSTRUCTION`
- `COLOUR_LOCK_INSTRUCTION`
- `MATERIAL_FIDELITY_INSTRUCTION`
- `EARRING_TYPE_PRESERVATION`
- `GENERIC_EARRING_PRESERVATION`
- `NEGATIVE_SPACE_INSTRUCTION` and `BEAD_CLUSTER_PRESERVATION_INSTRUCTION`, both from `earring_scale_reference_prompt`
- optionally `METAL_AFFIRMATION_INSTRUCTION` from the macro module

### 4.2 File and builder contract

```
backend/app/services/earring_stand_shot_prompt.py     # new, isolated; nothing imports it yet

build_stand_shot_prompt(
    earring_type: Optional[str] = None,      # "Hoop" | "Stud" | "Dangle" | None → generic
    stand_style: str = "t_bar",              # VALID_STAND_STYLES
    base_finish: str = "leatherette",        # VALID_BASE_FINISHES
    stand_tone: str = "auto",                # VALID_STAND_TONES
    backdrop: str = "soft_grey",             # VALID_BACKDROPS
) -> str
```

**Invariants**, which mirror the existing builders and what the tests and the prompt-verifier skill check:

1. Calling it with zero arguments returns the production prompt, because the workers call `builder()`.
2. It contains `REFERENCE_PRIORITY_MARKER` exactly once, so the manager doesn't append the block again.
3. It is deterministic: no randomness and no settings reads (`config.py` is being edited by another task).
4. An unknown enum value silently falls back to its default, as P4 does.
5. It imports only from `earring_ecommerce_prompt`, `earring_scale_reference_prompt` and (optionally) `earring_macro_shot_prompt`. It has no service or DB dependencies.
6. It returns `"\n\n".join(parts)`.
7. The intended aspect ratio is 4:5, passed via `context` by the caller rather than set inside the prompt.

### 4.3 Variable mapping (module-level constants)

Use the same `VALID_* / *_LABELS / *_INSTRUCTIONS` dispatch-dict shape as P4.

| Variable | `VALID_*` values (default first) | What it drives in the text |
|---|---|---|
| `stand_style` | `t_bar`, `ladder_bar`, `single_post_arm` | `t_bar`: one horizontal crossbar on a vertical upright with evenly spaced holes; `ladder_bar`: twin crossbars; `single_post_arm`: one L-arm, for a single earring |
| `base_finish` | `leatherette`, `velvet`, `matte_acrylic` | Weighted base: fine-grain leatherette, short-pile velvet, or frosted acrylic. All matte, so the base never mirrors the earring. |
| `stand_tone` | `auto`, `black`, `ivory`, `taupe` | `auto` = **reference-bound** wording: "a neutral stand tone that contrasts with the reference metal (dark stand for silver/white metal, ivory or taupe for yellow/rose gold)". Use this instead of injecting a metal variable, consistent with §1.4. |
| `backdrop` | `soft_grey`, `warm_beige`, `charcoal` | A seamless sweep, softly out of focus, with gentle falloff. Never `#FFFFFF`, which would blur the line between this shot and the white-bg product. |
| `earring_type` | `Hoop`, `Stud`, `Dangle`, `None` | Two lookups: (a) the existing `EARRING_TYPE_PRESERVATION`; (b) a new `MOUNTING_RULES[type]` (see §4.4). Jhumka and chandbali fall under `Dangle`, huggies under `Hoop`. |

There is no new constant for camera or lighting presets. They are fixed blocks, which matches the house convention.

### 4.4 Block order and content outline

The order follows the macro module: identity first, then task, then a short negative block. The content below is a **spec for the wording, not final copy**.

1. **`TASK` (inline):** "Earring on Stand Shot: a clean commercial studio display photograph of the EXACT earring(s) from the reference, hanging from a jewellery display stand." Include the line "This is product photography of the attached piece on a display stand. It is NOT a white-background cutout, NOT on-ear, NOT a tabletop flat-lay."
2. **`REFERENCE_PRIORITY_MARKER`**
3. **Product fidelity**, compact like P7 block 1, plus `ANTI_SYMMETRY_INSTRUCTION`.
4. **`EARRING_TYPE_PRESERVATION[t]`**, or the generic block.
5. **`COLOUR_LOCK_INSTRUCTION` + `MATERIAL_FIDELITY_INSTRUCTION`.** Optionally add the P7 `METAL_AFFIRMATION`, preferred given the silver-hallucination finding.
6. **`STAND_INSTRUCTIONS[stand_style]` + `BASE_INSTRUCTIONS[base_finish]` + tone:**
   - upright and crossbar in matte metal or wrapped finish
   - weighted base sitting flat on the sweep, with a soft contact shadow under the base only
   - the stand is cropped or kept secondary, taking about 25% of visual weight
   - no brand mark, text or logo on the stand
7. **`MOUNTING_RULES[earring_type]`**, the new block (same pattern as P3's `EARRING_PLACEMENT_RULES`):
   - **Dangle / hook:** the hook passes through a bar hole and the earring hangs straight down under gravity. Only the hook's front curve shows above the bar. **Do not invent the hidden back of the hook (anti-reconstruction).**
   - **Stud:** the post passes through the bar hole and the face sits flush and upright in front of the bar. The backing is hidden or shown exactly as in the reference.
   - **Hoop:** hangs over the bar at its closure point, with the round shape kept exactly. No deformation from the bar's contact.
   - **Generic (None):** "mount the earring on the stand the way its visible attachment allows; never alter the attachment to fit the stand."
8. **Pair / single branch:** this replaces the vague "bilateral symmetry".
   - "If the reference shows a PAIR: hang them side by side on the crossbar, the same distance either side of the upright, tops at the same height, both facing the camera. **This symmetry is about placement only.** Each earring keeps its own geometry exactly, including any asymmetry in the reference."
   - "If the reference shows ONE earring: hang it alone, centred on the crossbar. Never add a second earring."
9. **Camera (fixed):**
   - eye-level straight-on, or a slightly elevated angle of about 10°
   - 100mm macro-equivalent at about f/5.6, so the pair stays sharp across the bar width
   - backdrop softly blurred
   - earrings take about 60–70% of the frame height
   - add the house caveat: "visual guidance, not guaranteed camera parameters"
   - all jewellery sharp
10. **Lighting (fixed):**
    - large soft key from front-above, gentle fill, with a white bounce or flag for **reflection control on polished metal** (clean graduated highlights, no mirror-image of the stand or studio)
    - a subtle **rim or edge light** from behind to separate the metal from the backdrop
    - gemstones with natural facet sparkle and controlled brilliance; **no glowing stones, no exaggerated caustic or rainbow flares**
    - neutral white balance
    - the stand and backdrop colour must not tint the metal
11. **Backdrop:** `BACKDROP_INSTRUCTIONS[backdrop]`, a seamless sweep with soft falloff and no horizon or props.
12. **`NEGATIVE_SPACE_INSTRUCTION` + `BEAD_CLUSTER_PRESERVATION_INSTRUCTION`.** Gravity-hung drops especially risk fusing together.
13. **Short negatives** (keep this one short, following P7 rather than P3):
    - earring merged into or fused with the stand
    - hook fused to the bar
    - invented hook back
    - tilted or gravity-defying drops
    - pair at mismatched heights
    - extra or duplicated earrings
    - added or removed stones or prongs
    - warped metal
    - mirror-like reflections of the stand
    - stand colour cast on the metal
    - pure white background
    - human, ear, hand, mannequin bust
    - props, boxes, flowers, fabric drapes
    - text, logo, watermark
14. **Do not add a final verification checklist.** The P7 finding is that it doesn't work for image models.

The target length is about 6–8k characters, below P3/P5/P6 (12–15k). See `prompt-tokens-per-word-2026-09-28.md` for the cost per character.

### 4.5 Wiring later (do not do now; listed so the isolated file needs no rework)

- **Phase A, recommended:** add a standalone route `api/routes/earring_stand_shot.py`, cloned from `earring_professional_shot.py` with `archetype` replaced by `stand_style`/`base_finish`/`backdrop`, and register it in `main.py`. This has no effect on the paid pack.
- **Phase B, pack inclusion:** add `("Stand Display", "prompt_stand")` to `CATALOG_PACK_STYLES` and `"prompt_stand": build_stand_shot_prompt` to `style_prompt_builders`.
  - **Every 6-count assertion in `test_ecom_pack1_restore.py` breaks.** So do the "all 6 styles" acknowledgement copy and the "Delivered n/6" logic.
  - The daily spend reservation goes from 6 to 7 per pack, at the same ₹500 pack price, which is a margin hit; see `stage-costs-2026-09-30.md`.
  - `meta_whatsapp_service.py` currently has **uncommitted edits from another task**, so this must wait.
- **Tests for the new module:**
  - zero-argument call contains the marker exactly once
  - no forbidden imports (`PURE_WHITE…`, `ECOMMERCE_PRESENTATION…`, `ECOMMERCE_SHOT_*`)
  - no `#FFFFFF`
  - unknown enum values fall back to defaults
  - each `earring_type` pulls in its `MOUNTING_RULES` block
  - the single/pair clause is present
- **Dry run:** use the `prompt-verifier` skill (offline assembly per earring type, no paid calls) before any live generation.

---

## 5. Side note from this audit

**This audit left a stale lock file, now cleared.** Running `git status` from the sandbox refreshes the index and left an empty `.git/index.lock` (the sandbox cannot delete files). That would block git commands for the parallel tasks. I renamed it to `.git/index.lock.stale-claude-2026-10-01`, which unblocks git. There is an older `index.lock.stale-by-claude` from 29 Sep with the same cause. Both are empty and can be deleted by hand.
