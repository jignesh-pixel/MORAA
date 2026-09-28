# Live image-generation prompts — reference

Extracted 2026-09-28 from `main` @ c206a39 by calling each production prompt builder exactly as the live WhatsApp workers call it (no arguments). The text below is the builder's return value, verbatim. Nothing was edited.

**Effective settings:** `EARRING_PROMPT_VERSION = v1` (the `app/config.py` default; not set in `.env`), and `MAX_STYLES_PER_PACK = None`, so the pack runs all 6 styles. `PRIMARY_IMAGE_PROVIDER = gemini`, model `gemini-3.1-flash-image`.

**What reaches the model:** every prompt below already contains the `REFERENCE IMAGE PRIORITY` marker, so `ImageGenerationManager` appends nothing, and neither worker passes a marketplace. On Gemini, the request is sent as `[REFERENCE_IMAGE_ANCHOR, <customer photo>, <prompt>]`. On the OpenAI fallback (images.edit), `OPENAI_IDENTITY_ANCHOR` is prepended to the prompt. Both anchors are reproduced in the appendix.

**Constant lists** name the module-level constants whose full text appears verbatim in the assembled prompt. Text that the builder writes inline, or builds per earring type, belongs to the builder function itself.

## Contents

1. ₹50 Clean Studio Shot (white background)
2. E-Com Pack 1 (Full Catalog Pack): 6 shots, in delivery order
3. Appendix: provider anchors

## 1. ₹50 Clean Studio Shot (white background)

### Clean Studio Shot

| | |
|---|---|
| File | `backend/app/services/ecommerce_shot_prompt.py` |
| Builder | `build_ecommerce_shot_prompt()` (line 76) |
| Called from | `backend/app/services/meta_whatsapp_service.py` — the white-background worker (`product_code = PRODUCT_WHITE_BG`), `prompt=build_ecommerce_shot_prompt()` |
| Aspect ratio | 1:1 |
| Price | `WHITE_BG_PRICE_RUPEES` (default ₹50) |
| Length / md5 | 6635 chars / `813bf8e088e47e0aa73b47f46028652b` |
| Constants used | `PRODUCT_PRESERVATION_CONSTRAINTS` (product_fidelity.py:521), `REFERENCE_PRIORITY_BLOCK` (product_fidelity.py:561), `ECOMMERCE_SHOT_TASK` (ecommerce_shot_prompt.py:23), `ECOMMERCE_SHOT_STUDY` (ecommerce_shot_prompt.py:31), `ECOMMERCE_SHOT_ISOLATE` (ecommerce_shot_prompt.py:42), `ECOMMERCE_SHOT_STAGING` (ecommerce_shot_prompt.py:50), `ECOMMERCE_SHOT_OUTPUT` (ecommerce_shot_prompt.py:68) |

````text
TASK: Create ONE professional e-commerce catalogue photograph of the EXACT earring(s) in the attached customer photo, ready to be the main image of an Amazon / Myntra / website product listing.
The attached photo is a raw phone picture. Re-photograph that same piece in a clean studio — this is product photography, not design.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRIORITY RULE: REFERENCE PRODUCT IDENTITY > SCENE / CREATIVE INSTRUCTION.
The uploaded image is the single authoritative source of product truth.  The text prompt may describe the desired scene, presentation, or styling — but it must never override, reinterpret, or replace the physical identity of the product shown in the reference image.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.
• Symmetry or asymmetry: preserve the exact visible symmetry — do not symmetrise an asymmetric design or vice versa.

WHAT YOU MUST NOT CHANGE:
• Do not redesign or reinterpret the product.
• Do not simplify or abstract the design.
• Do not add stones, decorative elements, or features not shown.
• Do not remove stones, decorative elements, or features that are shown.
• Do not change stone arrangement, spacing, or placement.
• Do not change proportions or geometry.
• Do not change metal colour, finish, or texture.
• Do not change attachment structure or type.
• Do not change the product's visible size relative to the reference — do not enlarge or shrink it.
• Do not turn a single piece into a pair or a pair into a single piece.
• Do not create a 'similar' or 'inspired' variation.

WHAT YOU MAY CHANGE (PRESENTATION ONLY):
• Background and environment.
• Lighting quality, direction, and colour temperature.
• Camera angle, focal length, and depth of field.
• Composition and framing.
• Sharpness, resolution, and commercial presentation quality.

This is professional product photography of the exact uploaded product — not creative generation, not a redesign, and not a new interpretation.

STEP 1 — STUDY THE REFERENCE FIRST (do not write any text in the image):
• Earring type (stud, drop, dangle, jhumka, hoop, chandbali, ear cuff …) and whether the photo shows ONE earring or a PAIR — keep exactly that number.
• Every stone: count, cut/shape, size, colour and exact position.
• Metal: colour (yellow gold, rose gold, silver, oxidised, etc.), finish and texture.
• Pearls, beads, chains, hanging drops and their counts; hooks, posts, screw backs or clasps.
• Engravings, filigree, meenakari/enamel colours, cut-outs and surface detail.

STEP 2 — ISOLATE THE JEWELLERY:
Remove everything that is not the earring: the original background, hands and fingers, ears, display cards, packaging, tags, tables, fabric and clutter. Never remove or trim any part of the earring itself (hooks, posts, chains, drops are part of the product).

STEP 3 — STAGE IT FOR THE CATALOGUE:
• Background: seamless pure white #FFFFFF (RGB 255,255,255) edge to edge — no grey cast, gradient, vignette, horizon, texture or props.
• Placement: the earring(s) centred, upright and front-facing as worn, filling about 80–85% of the frame with even margins; a pair sits side by side at the same height and scale.
• Lighting: bright professional studio softbox lighting (key + fill), even and neutral white balance; crisp specular highlights on metal and natural sparkle in stones without blown-out areas.
• Shadow: a soft, natural, light-grey drop shadow directly beneath the jewellery, as if resting just above a white sweep — subtle and diffused, never harsh or dark. Everywhere else the background stays pure white.
• Focus: tack-sharp from front to back, high resolution, true-to-life colours.
• Nothing else in the image: no model, no ear, no hand, no mannequin, no stand, no box, no text, no logo, no watermark, no border.

CRITICAL PRODUCT PRESERVATION CONSTRAINTS (NON-NEGOTIABLE):
1. EXACT PRODUCT GEOMETRY: Preserve the EXACT overall shape, exact dimensions, exact proportions, and exact silhouette of this product. Do not stretch, compress, enlarge, or reduce any element.
2. STONE PLACEMENT: Preserve every gemstone position, gemstone count, gemstone colors, and gemstone arrangement exactly as they appear. Do not add new gemstones. Do not remove existing gemstones. Do not change gemstone colors or shapes.
3. METAL DETAILS: Preserve all carvings, texture, finish, engravings, and craftsmanship details exactly. Do not smooth over intricate work. Do not simplify complex patterns.
4. HANGING ELEMENTS: Preserve all dangling beads, bead count, bead positions, and chain positions exactly. Do not add or remove any hanging elements.
5. PRODUCT IDENTITY: The generated image MUST represent the EXACT same product, not a redesigned or inspired variation. This is a photography task, not a design task.
6. WHAT YOU MAY CHANGE (ONLY): Studio lighting quality, camera lens quality, background, shadows, reflections, sharpness, resolution, commercial presentation quality.
7. WHAT YOU MUST NOT CHANGE: The jewellery piece itself — its shape, stones, metalwork, engravings, proportions, colors, textures, or any visible decorative elements.
8. ANTI-REDESIGN RULE: This is NOT a design task. Do NOT redesign the product. Do NOT invent new elements. Do NOT create a new interpretation. Reproduce the EXACT product in a professional studio setting.

OUTPUT RULE: exactly ONE photorealistic image in which the customer's earring is clearly visible and is the only subject, on pure white with a soft drop shadow. A blank or empty white image, or a different/redesigned earring, is a failed result.
````

## 2. E-Com Pack 1 (Full Catalog Pack): 6 shots

The worker is `process_whatsapp_catalog_pack()` in `backend/app/services/meta_whatsapp_service.py`. The shot order comes from `CATALOG_PACK_STYLES` (line 36), and each `prompt_type` maps to its builder through `style_prompt_builders`. Every shot is called with `builder()` and sent at aspect ratio 4:5. There are 6 shots, not 7.

### 2.1 Clean E-Commerce

| | |
|---|---|
| File | `backend/app/services/earring_ecommerce_prompt.py` |
| Builder | `build_earring_ecommerce_prompt()` (line 443) |
| Pack slot / key | 1 of 6 — `"Clean E-Commerce"` / `prompt_ecommerce` |
| Version | `build_earring_ecommerce_prompt` dispatches to `build_earring_ecommerce_prompt_v1` (v2 exists but is off) |
| Length / md5 | 9746 chars / `2987ad4009dcb7d1c5e22aaad4587333` |
| Constants used | `PURE_WHITE_BACKGROUND_INSTRUCTION` (earring_ecommerce_prompt.py:41), `GENERIC_EARRING_PRESERVATION` (earring_ecommerce_prompt.py:79), `MATERIAL_FIDELITY_INSTRUCTION` (earring_ecommerce_prompt.py:88), `ANTI_SYMMETRY_INSTRUCTION` (earring_ecommerce_prompt.py:111), `INPUT_CLEANUP_INSTRUCTION` (earring_ecommerce_prompt.py:141), `ANGLE_PRESERVATION_INSTRUCTION` (earring_ecommerce_prompt.py:169), `ECOMMERCE_PRESENTATION_INSTRUCTION` (earring_ecommerce_prompt.py:183), `COLOUR_LOCK_INSTRUCTION` (earring_ecommerce_prompt.py:200), `ANTI_REDESIGN_INSTRUCTION` (earring_ecommerce_prompt.py:230) |

````text
BACKGROUND SPECIFICATION — 100% PURE SOLID WHITE (#FFFFFF, RGB 255, 255, 255) (NON-NEGOTIABLE):
• The background must be completely, uniformly, and seamlessly solid pure white (#FFFFFF, RGB 255, 255, 255).
• Amazon India & Global Marketplace Main Listing standard: pure white infinite studio cutout.
• ZERO physical tabletop, zero floor surface, zero marble/stone/wood texture, zero podium, zero pedestal, zero acrylic block.
• ZERO horizon line, zero room corner, zero wall-to-floor transition.
• ZERO dark cast shadows, zero vignette, zero grey edge drop-off, zero ambient gradient.
• The earrings must appear cleanly floating or upright in an infinite, pure white void with ultra-crisp edges.
• Only minimal, natural contact occlusion lighting underneath the jewelry is permitted; the surrounding canvas must remain 100% pristine solid white.

CRITICAL: The earrings in the output MUST be an exact 1:1 physical replica of the earrings provided in the reference image. Retain exact stone count, stone shapes (e.g., baguette, marquise, pear, round), prong setting structure, metal tone, and earring silhouette. DO NOT alter the core jewelry design or invent alternate motifs.

Remove all retail packaging, polybags, display cards, plastic film, human fingers, and tabletop surfaces. Extract the jewelry piece with pristine studio fidelity directly onto pure solid white (#FFFFFF).

TASK: Generate a single e-commerce main image for a Fashion Jewellery Earring product on a seamless pure solid white (#FFFFFF) background. The uploaded reference image is the authoritative source of truth for the actual product.

REFERENCE IMAGE PRIORITY: MAXIMUM

ANTI-REDESIGN RULE (NON-NEGOTIABLE):
This is a PRODUCT PHOTOGRAPHY task, NOT a design task.
You are photographing the EXACT uploaded product in a professional pure white background setting. You are NOT designing a new earring, creating an inspired variation, or improving a product.
The generated image must show the EXACT same product — same shape, same stones, same metal, same proportions, same craftsmanship, same asymmetry, same imperfections.
Only the presentation changes: background to pure solid white #FFFFFF, lighting, composition, and commercial marketplace quality.

CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):
The reference image's actual visible asymmetry IS part of the product identity. If the reference shows asymmetry, the output MUST preserve that asymmetry exactly.

DO NOT:
• Make both sides identical simply because symmetry looks more aesthetically pleasing.
• Normalise geometry — do not straighten curves, regularise shapes, or correct perceived manufacturing imperfections.
• Beautify the product — do not smooth surfaces, round edges, or improve proportions beyond what the reference shows.
• Correct asymmetry — do not mirror one side to match the other.
• Adjust proportions — do not elongate, compress, or resize any element for visual balance.
• Invent details — do not add stones, engravings, filigree, or decorative elements not visible in the reference.
• Remove genuine details — do not remove elements that appear irregular, imperfect, or asymmetric.
• Convert an asymmetric product into a symmetric one.
• Treat visible irregularity as an error to be corrected.

The product in the reference is the ground truth. Any asymmetry, irregularity, or imperfection in the reference IS the product. Reproduce it faithfully.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.

EARRING TYPE: Preserve the exact earring type as shown in the reference. Do not convert one earring type into another (e.g. hoop to stud, stud to dangle, dangle to hoop). Preserve the complete structure including all attachment mechanisms, hanging elements, and connecting components.

MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):
The reference image is the authoritative source for all material appearance. Preserve EXACTLY as shown:
• Metal colour — do not convert silver to gold, gold to silver, brass to gold, or any other material substitution.
• Metal finish — preserve polished, brushed, matte, hammered, or any other surface treatment exactly.
• Plating appearance — preserve gold plating, silver plating, or any coating as shown.
• Gemstone colour — preserve every stone's exact colour without oversaturation or artificial brightening.
• Pearl appearance — preserve lustre, colour, and surface quality.
• Bead appearance — preserve colour, size, and arrangement.
• Reflectivity — preserve the natural reflectivity of the metal and stones.
Do NOT oversaturate colours. Do NOT artificially brighten the jewellery. A white background must NOT be interpreted as white jewellery. The material in the reference is the truth.

COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):
The reference image is the sole authority for the product's actual material and colour.
Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, tone-shift, or transform the product's metal or stone colours.
Silver must remain silver.
Gold must remain gold.
Gold plating must remain gold plating.
Silver plating must remain silver plating.
Platinum must remain platinum.
Brass must remain brass.
925 silver must remain 925 silver.
Blue stones must remain blue.
Red stones must remain red.
Green stones must remain green.
Clear stones must remain clear.
Do not infer a different material from studio lighting or reflections.
Lighting may change illumination ONLY.
Lighting must NEVER change the perceived underlying product material or colour.
The colour temperature of the output MUST match the reference. If the reference shows cool/silver tones, the output must NOT warm them to gold. If the reference shows warm tones, the output must NOT cool them to silver.

INPUT CLEANUP (NON-NEGOTIABLE):
The reference image may contain photographic distractions that must be removed from the e-commerce output. Remove:
• Human hand, fingers, or body parts holding the earring.
• Jewellery display card, backing card, or packaging.
• Surface, counter, or table the earring is resting on.
• Background distractions, unrelated objects, clutter.
• Shadows cast on the background by the earring or hand.
• Inconsistent lighting artefacts.

CRITICAL: When removing a card, backing, or hand, NEVER remove a component that is actually part of the jewellery product. An earring hook, post, clasp, chain, connector, lever-back, or decorative component must NOT be mistaken for removable background material.
When in doubt, PRESERVE the component — it may be part of the jewellery.

ANTI-RECONSTRUCTION RULE:
If a section of the product is hidden behind a hand, angle, or other object in the reference, do NOT reconstruct or invent that section. Preserve only what is visibly present in the reference. Omitted geometry is preferable to fabricated geometry. Do not hallucinate product details that are not visible in the source image.

ANGLE & VIEW PRESERVATION:
Preserve the meaningful visible orientation of the reference wherever possible. Do NOT rotate the product to make the composition prettier. Do NOT convert a front-facing product into an artificial perspective. Do NOT change the visible geometry by changing the viewing angle. If the source image is photographed at an angle, preserve the product's actual structure while cleaning the presentation. The earring's orientation in the reference is the correct orientation for the e-commerce output.

E-COMMERCE PRESENTATION (PRESENTATION ONLY — not product-identity):
Generate a professional, high-end e-commerce main catalog image:
• The product is the absolute sole visual focus, set on seamless pure solid white (#FFFFFF).
• Product centered appropriately with comfortable listing margins (occupying ~85% of the frame).
• Sharp, pristine product details with accurate material and metal rendering.
• High-key, bright, balanced commercial studio lighting across the entire piece.
• No reflections of photographers, equipment, or unnatural colored lights.
• No distracting props, stands, acrylic holders, text, watermarks, or overlays.
• No packaging, no jewellery card, no display backing.
• Accurate scale — the earrings should appear at realistic size relative to their actual dimensions.
• The colour temperature of the output MUST match the reference. Silver metals must stay silver. Gold metals must stay gold. Do NOT warm or cool the product's natural colour.

FINAL OUTPUT RULE:
The earring must appear on a 100% pure solid white background (#FFFFFF, RGB 255, 255, 255) ONLY. No tabletop, no surface gradient, no grey vignette, no floor reflection, no display card, no human hand. The earring is the ONLY object in the entire image.
````

### 2.2 Close-up on Ear

| | |
|---|---|
| File | `backend/app/services/earring_close_up_ears_prompt.py` |
| Builder | `build_close_up_ears_prompt()` (line 25) |
| Pack slot / key | 2 of 6 — `"Close-up on Ear"` / `prompt_close_up` |
| Length / md5 | 8898 chars / `4c84d6ab246237b0f5cd392186792c68` |
| Constants used | `NEGATIVE_SPACE_INSTRUCTION` (earring_scale_reference_prompt.py:200) |

````text
CRITICAL: The earrings in the output MUST be an exact 1:1 physical replica of the earrings provided in the reference image. Retain exact stone count, stone shapes (e.g., baguette, marquise, pear, round), prong setting structure, metal tone, and earring silhouette. DO NOT alter the core jewelry design or invent alternate motifs.

Remove all retail packaging, polybags, display cards, plastic film, and human fingers. Extract the jewelry piece with pristine studio fidelity.

TASK: Close Up Ears.
Generate ONE realistic, premium e-commerce photograph showing the exact earring from the uploaded reference image naturally worn on a woman's ear in an extreme macro close-up.
This is an ON-EAR PRESENTATION task — the image MUST contain a visible human ear with the earring attached.
This is NOT a standalone product image. This is NOT a white-background catalogue shot. This is NOT a floating jewellery render.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRODUCT FIDELITY — STRUCTURAL — HIGHEST PRIORITY (NON-NEGOTIABLE):
The uploaded reference image is the sole authoritative source for the jewellery product. You are placing the EXACT jewellery from the reference onto a woman's ear. You are NOT creating new jewellery.

Preserve EXACTLY as shown in the reference:
• Overall silhouette and outline — reproduce the exact visible shape.
• Geometry — the exact form, curves, angles, and structural lines.
• Proportions — the exact length-to-width ratio and relative sizing of every component.
• Stone positions — preserve every visible stone's exact location, spacing, and spatial relationship to the metal structure.
• Stone shapes — preserve the exact visible stone shapes (baguette, round, teardrop, marquise, etc.). Do NOT round rectangular stones. Do NOT change cut styles.
• Stone colours — preserve every stone's exact colour without oversaturation or substitution.
• Metal appearance — preserve the exact metal colour, finish, texture, and surface quality.
• Connector relationships — preserve every visible link, loop, jump ring, wire, and structural connection between components.
• Hanging elements — preserve every visible dangling bead, pearl, chain, teardrop, and decorative element.
• Component count — preserve the exact number of every type of visible component.

Do not add, remove, substitute, merge, split, recolour, resize, or invent any jewellery component. If any product detail is unclear or partly hidden in the reference, do not fabricate a different detail. If presentation conflicts with product fidelity, PRESERVE PRODUCT FIDELITY. The jewellery is never modified for composition reasons.

NEGATIVE SPACE — HARD REQUIREMENT (NON-NEGOTIABLE):
The reference jewellery contains intentional OPEN / EMPTY areas. These are structural features, not gaps to fill.

You MUST preserve every open/hollow region:
• Hollow crescent or open wireframe structures must remain hollow.
• Open centres of circular or geometric frames must remain empty.
• Spaces between hanging elements must remain separate.
• Gaps between bead clusters must remain visible.
• Openwork, filigree, or cut-out patterns must stay open.

DO NOT fill any internal void with:
• Gold or metal material
• Skin or flesh tone
• Background colour
• Gemstone material
• Decorative texture or pattern
• Shadow or shading

The empty space IS part of the jewellery geometry. Filling it changes the product identity.

BEAD AND PEARL CLUSTER PRESERVATION (NON-NEGOTIABLE):
If the reference jewellery contains bead clusters, pearl groups, or dangling bead arrangements:

• Each individual bead/pearl must remain visually distinguishable — no merging, fusing, or melting.
• Bead clusters must retain their individual separation — gaps between beads are part of the design.
• Large faceted teardrop drops must remain individually identifiable — each drop is a separate component.
• Hanging bead arrangements must preserve the exact count and relative positioning.
• Bead sizes must match the reference — do not enlarge small beads or shrink large ones.
• Natural gravity may affect POSITION in the scene, but must NOT alter the PRODUCT STRUCTURE.

DO NOT allow:
• Fused beads that merge into a single mass
• Melted or blob-like bead clusters
• Missing drops that were present in the reference
• Invented drops that were not in the reference
• Random bead blobs replacing structured clusters

TOP STUD / EAR ATTACHMENT (CRITICAL):
Preserve the exact top stud, cluster, or attachment point from the reference. It must remain:
• Clearly visible and sharply defined
• Structurally intact — exact shape, exact stone placement
• Correctly positioned at the ear piercing point
• Physically attached through the earlobe piercing

The stud should appear naturally threaded through the piercing hole — not floating beside the ear, not merged into skin, not blurred into the background.

DO NOT:
• Blur the stud into skin texture
• Merge it into the earlobe surface
• Turn it into a generic round stud
• Change its shape or geometry
• Remove its central stone or decorative elements

ON-EAR COMPOSITION (CRITICAL — THIS IS THE PRIMARY REQUIREMENT):
The image MUST show a clearly visible human ear with the exact reference earring naturally attached to the earlobe.
• The ear must be the dominant structural element in the frame.
• The earring must be visibly pierced through or hooked onto the earlobe in a realistic, anatomically correct manner.
• Show realistic attachment: the post, hook, or wire must pass through the piercing hole or wrap around the earlobe naturally.
• Maintain natural gravity — dangle earrings must hang downward; studs must sit flush; hoops must arc naturally.
• The earring must be proportional to the ear — correct scale, correct distance from the lobe, correct visual weight.
• Hair should be tucked behind or away from the ear to keep the earring fully visible.
• Skin texture must be natural with realistic pores, tone, and subsurface scattering.
• The ear and earring must occupy the majority of the frame.

MACRO PHOTOGRAPHY STYLE:
Use an extreme macro close-up perspective — as if shot with an 85-100mm macro lens at approximately f/4 depth of field.
• The earring and earlobe must be tack-sharp with visible metal reflections, gemstone facets, and surface detail.
• Shallow depth of field is acceptable for background blur, but IMPORTANT: do not let shallow depth of field blur any jewellery component — all earring elements must remain sharp and inspectable.
• Partial side profile is acceptable. Full face is NOT required and should be cropped out or kept non-identifiable.
• The complete earring must be fully within the frame — no clipping of hooks, drops, or dangling elements.

BACKGROUND:
Use a soft, neutral, out-of-focus studio background — warm grey, soft beige, or gentle gradient. The background must be secondary and non-distracting.
Do NOT use a pure white background. Do NOT use a flat solid colour. The presence of a human ear makes this a portrait/lifestyle composition, not a product-only catalogue shot.

E-COMMERCE PRESENTATION:
Professional studio-quality lighting with soft, even illumination on the ear and earring. Natural skin texture. Sharp jewellery detail. Realistic depth and perspective. Subtle realistic shadows from the earring onto the earlobe. No props, flowers, fabric, decorative objects, heavy makeup, text, logos, watermarks, or additional jewellery. The ear and the exact reference earring are the dominant visual elements.

DO NOT produce any of the following:
• Isolated product on a plain background — this is an on-ear image.
• Plain white background — this is a portrait composition.
• Floating earring with no ear — the earring MUST be attached to a visible human ear.
• Product-only image without any person — a human ear is mandatory.
• Still life or catalogue-style product display.
• Jewellery displayed beside the ear rather than worn on it.
• Detached or dislocated jewellery.
• Redesigned or alternative jewellery — only the exact reference product.
• Added or missing components — preserve exact component count.
• Changed gemstone cuts, colours, or sizes.
• Changed bead shapes, sizes, or arrangement.
• Filled negative space — hollow areas must remain hollow.
• Fused or merged bead clusters — every bead must be separate.
• Blurred or deformed jewellery geometry.
• Melted or blob-like jewellery rendering.
• Additional jewellery items on the ear or nearby.
• Full-face portrait — the ear and earring must dominate, face should be cropped or minimal.

FINAL OUTPUT: Return ONE realistic Close Up Ears e-commerce image showing a woman's ear with the exact reference earring naturally worn. The result must look like professional macro jewellery photography — a premium earring-on-ear lifestyle shot that retains the EXACT identity, geometry, negative spaces, bead clusters, gemstones, connectors, and visible construction of the uploaded reference earring.
````

### 2.3 Scale Reference

| | |
|---|---|
| File | `backend/app/services/earring_scale_reference_prompt.py` |
| Builder | `build_scale_reference_prompt()` (line 320) |
| Pack slot / key | 3 of 6 — `"Scale Reference"` / `prompt_scale_reference` |
| Length / md5 | 15137 chars / `99e0ccc8d549b277c4210526e9ac0bb3` |
| Constants used | `GENERIC_EARRING_PRESERVATION` (earring_ecommerce_prompt.py:79), `MATERIAL_FIDELITY_INSTRUCTION` (earring_ecommerce_prompt.py:88), `ANTI_SYMMETRY_INSTRUCTION` (earring_ecommerce_prompt.py:111), `COLOUR_LOCK_INSTRUCTION` (earring_ecommerce_prompt.py:200), `PRODUCT_FIDELITY_ABSOLUTE` (earring_scale_reference_prompt.py:57), `HAND_REQUIREMENTS_INSTRUCTION` (earring_scale_reference_prompt.py:78), `EARRING_PLACEMENT_RULES` (earring_scale_reference_prompt.py:101), `SIZE_SCALE_ACCURACY_INSTRUCTION` (earring_scale_reference_prompt.py:143), `OCCLUSION_ANTI_RECONSTRUCTION_INSTRUCTION` (earring_scale_reference_prompt.py:162), `DIMENSION_INTEGRITY_INSTRUCTION` (earring_scale_reference_prompt.py:179), `NEGATIVE_SPACE_INSTRUCTION` (earring_scale_reference_prompt.py:200), `BEAD_CLUSTER_PRESERVATION_INSTRUCTION` (earring_scale_reference_prompt.py:227), `NEGATIVE_FAILURE_PREVENTION` (earring_scale_reference_prompt.py:254), `FINAL_VERIFICATION_CHECKLIST` (earring_scale_reference_prompt.py:295) |

````text
Create a clean, premium e-commerce scale-reference photograph using the uploaded original earring image as the ONLY product reference.

PRIMARY OBJECTIVE:
Show the true physical size, proportion, silhouette, and visual scale of the EXACT SAME EARRING by placing it naturally on a single open human hand.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRODUCT FIDELITY — ABSOLUTE PRIORITY:
Reproduce the exact original earring from the reference image.
Preserve 100% of the original design, silhouette, dimensions, proportions, metal color, finish, stones, gemstones, beads, motifs, links, hooks, pins, backs, clasps, and every visible structural detail.

ZERO redesign.
ZERO beautification that changes the product.
ZERO addition, removal, substitution, duplication, simplification, or reinterpretation of any product component.

Do not make the earring larger, smaller, thicker, thinner, longer, shorter, wider, narrower, heavier, or more delicate than the reference.
The hand is ONLY a physical scale reference. The jewellery itself must remain geometrically and proportionally constant.

ANTI-REDESIGN RULE (NON-NEGOTIABLE):
This is a PRODUCT PHOTOGRAPHY task, NOT a design task.
You are photographing the EXACT uploaded product in a scale-reference composition. You are NOT designing a new earring, creating an inspired variation, or improving a product.
The generated image must show the EXACT same product — same shape, same stones, same metal, same proportions, same craftsmanship, same asymmetry, same imperfections.
Only the presentation changes: background, lighting, composition, scale reference (hand), and commercial quality.

CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):
The reference image's actual visible asymmetry IS part of the product identity. If the reference shows asymmetry, the output MUST preserve that asymmetry exactly.

DO NOT:
• Make both sides identical simply because symmetry looks more aesthetically pleasing.
• Normalise geometry — do not straighten curves, regularise shapes, or correct perceived manufacturing imperfections.
• Beautify the product — do not smooth surfaces, round edges, or improve proportions beyond what the reference shows.
• Correct asymmetry — do not mirror one side to match the other.
• Adjust proportions — do not elongate, compress, or resize any element for visual balance.
• Invent details — do not add stones, engravings, filigree, or decorative elements not visible in the reference.
• Remove genuine details — do not remove elements that appear irregular, imperfect, or asymmetric.
• Convert an asymmetric product into a symmetric one.
• Treat visible irregularity as an error to be corrected.

The product in the reference is the ground truth. Any asymmetry, irregularity, or imperfection in the reference IS the product. Reproduce it faithfully.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.

EARRING TYPE: Preserve the exact earring type as shown in the reference. Do not convert one earring type into another (e.g. hoop to stud, stud to dangle, dangle to hoop). Preserve the complete structure including all attachment mechanisms, hanging elements, and connecting components.

MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):
The reference image is the authoritative source for all material appearance. Preserve EXACTLY as shown:
• Metal colour — do not convert silver to gold, gold to silver, brass to gold, or any other material substitution.
• Metal finish — preserve polished, brushed, matte, hammered, or any other surface treatment exactly.
• Plating appearance — preserve gold plating, silver plating, or any coating as shown.
• Gemstone colour — preserve every stone's exact colour without oversaturation or artificial brightening.
• Pearl appearance — preserve lustre, colour, and surface quality.
• Bead appearance — preserve colour, size, and arrangement.
• Reflectivity — preserve the natural reflectivity of the metal and stones.
Do NOT oversaturate colours. Do NOT artificially brighten the jewellery. A white background must NOT be interpreted as white jewellery. The material in the reference is the truth.

COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):
The reference image is the sole authority for the product's actual material and colour.
Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, tone-shift, or transform the product's metal or stone colours.
Silver must remain silver.
Gold must remain gold.
Gold plating must remain gold plating.
Silver plating must remain silver plating.
Platinum must remain platinum.
Brass must remain brass.
925 silver must remain 925 silver.
Blue stones must remain blue.
Red stones must remain red.
Green stones must remain green.
Clear stones must remain clear.
Do not infer a different material from studio lighting or reflections.
Lighting may change illumination ONLY.
Lighting must NEVER change the perceived underlying product material or colour.
The colour temperature of the output MUST match the reference. If the reference shows cool/silver tones, the output must NOT warm them to gold. If the reference shows warm tones, the output must NOT cool them to silver.

HAND REQUIREMENT (NON-NEGOTIABLE):
Show exactly ONE natural human hand.

• Use a clean, soft, feminine adult hand with a delicate appearance suitable for premium women's jewellery.
• Hand should appear well-groomed and naturally manicured, with clean nails.
• Skin should look realistic, healthy, and natural.
• Avoid rough, heavily textured, masculine-looking, muscular, or excessively veined hands.
• No jewellery, rings, bracelets, watches, tattoos, or other accessories on the hand.
• No second hand anywhere in the frame.
• No wrist styling or unnecessary body parts.

The hand is ONLY a physical scale reference.
THE EARRING IS THE HERO SUBJECT.

EARRING PLACEMENT RULES (NON-NEGOTIABLE):
Determine placement from the actual earring construction visible in the reference image.

IF DANGLE / DROP EARRING:
• Place the earring completely flat and naturally resting on the open palm.
• The entire earring must rest ON TOP of the skin.
• Do NOT pierce, insert, push, embed, or penetrate any part of the earring into the skin.
• Metal pins, hooks, posts, wires, or findings must remain visibly above the skin and must NEVER appear to enter or puncture the palm.
• Do not bend or reshape the earring to fit the hand.

IF STUD EARRING:
• Place the stud naturally near the fingertips / finger area.
• It must be resting on top of the hand, not inserted into the skin.
• The post/pin must remain completely outside the skin.
• NEVER depict the stud as pierced or being worn.

IF HOOP / LOOP EARRING:
• Hold the hoop naturally and gently between the fingers of the SAME hand.
• The fingers may lightly support the earring only.
• Do not squeeze, deform, stretch, or reshape the hoop.
• Preserve the exact original circular/curved geometry and dimensions.

GENERAL PLACEMENT RULE:
The earring is a physical object being displayed for size comparison, NOT being worn.
No piercing.
No skin penetration.
No pin entering skin.
No clasp inserted into skin.
No fingers passing through the earring unless the reference construction requires natural holding of a hoop/loop.

SIZE & SCALE ACCURACY (NON-NEGOTIABLE):
• Maintain the exact physical proportion of the original earring.
• Do not use AI-generated assumptions to alter its size.
• The hand provides visual scale only; it must NOT cause the jewellery to be resized or redesigned.
• Preserve the relative dimensions between every component of the earring.
• Do not exaggerate the jewellery for visual impact.
• Do not minimize it to make it appear delicate.
• The generated image must communicate the product's true apparent physical scale as faithfully as possible.

PRODUCT FIDELITY HAS PRIORITY OVER SCALE PRESENTATION.

NEGATIVE SPACE — HARD REQUIREMENT (NON-NEGOTIABLE):
The reference jewellery contains intentional OPEN / EMPTY areas. These are structural features, not gaps to fill.

You MUST preserve every open/hollow region:
• Hollow crescent or open wireframe structures must remain hollow.
• Open centres of circular or geometric frames must remain empty.
• Spaces between hanging elements must remain separate.
• Gaps between bead clusters must remain visible.
• Openwork, filigree, or cut-out patterns must stay open.

DO NOT fill any internal void with:
• Gold or metal material
• Skin or flesh tone
• Background colour
• Gemstone material
• Decorative texture or pattern
• Shadow or shading

The empty space IS part of the jewellery geometry. Filling it changes the product identity.

BEAD AND PEARL CLUSTER PRESERVATION (NON-NEGOTIABLE):
If the reference jewellery contains bead clusters, pearl groups, or dangling bead arrangements:

• Each individual bead/pearl must remain visually distinguishable — no merging, fusing, or melting.
• Bead clusters must retain their individual separation — gaps between beads are part of the design.
• Large faceted teardrop drops must remain individually identifiable — each drop is a separate component.
• Hanging bead arrangements must preserve the exact count and relative positioning.
• Bead sizes must match the reference — do not enlarge small beads or shrink large ones.

DO NOT allow:
• Fused beads that merge into a single mass
• Melted or blob-like bead clusters
• Missing drops that were present in the reference
• Invented drops that were not in the reference
• Random bead blobs replacing structured clusters

OCCLUSION & ANTI-RECONSTRUCTION (NON-NEGOTIABLE):
The hand must not intentionally hide important earring details.

If any part of the earring becomes naturally obscured by the hand:
DO NOT reconstruct, invent, or assume the hidden geometry.
Do not use symmetry to guess the hidden side.
Do not invent hidden stones, beads, connectors, hooks, or decorative elements.
Preserve only the product structure supported by the uploaded reference.

VISIBLE REFERENCE DATA ALWAYS HAS PRIORITY OVER AI ASSUMPTION.

DIMENSION INTEGRITY:
Use the hand only as a visual scale reference.
If verified product dimensions are separately provided, those verified dimensions are authoritative.

If verified dimensions are NOT provided:
• Do not invent millimetre measurements.
• Do not invent centimetre measurements.
• Do not add dimension labels.
• Do not create measurement callouts.
• Do not display fake numerical values.
• Do not claim an exact measurement from visual estimation.

The image communicates relative physical scale only.
Do not alter the jewellery to satisfy an assumed measurement.

BACKGROUND (NON-NEGOTIABLE):
Background must be ABSOLUTELY PURE WHITE:
RGB: 255, 255, 255
HEX: #FFFFFF

No off-white.
No cream.
No ivory.
No grey.
No warm-white.
No gradient.
No visible studio wall.
No textured surface.
No background props.
White should remain #FFFFFF throughout the visible background.

LIGHTING:
Clean neutral e-commerce studio lighting.
• Soft, even illumination.
• Accurate product color reproduction.
• Controlled natural-looking contact shadow beneath the jewellery/hand where physically appropriate.
• No dramatic shadows.
• No colored lighting.
• No cinematic color grading.
• No excessive highlights that hide product details.

Lighting must reveal the product. Lighting must NOT beautify, recolour, redesign, or alter the jewellery.
The hand and earring should appear naturally illuminated by the same scene lighting.

COMPOSITION:
• Single hand + exact original earring ONLY.
• Jewellery remains the visual focal point.
• Hand provides contextual scale without dominating the composition.
• Clean centered/controlled e-commerce framing.
• No additional objects or decorative props.
• No ruler, measuring tape, coins, cards, flowers, fabric, boxes, or accessories.

Do not change the earring's meaningful viewing orientation merely to improve composition.

IMAGE QUALITY:
• Photorealistic commercial e-commerce photography.
• Extremely sharp jewellery details.
• Preserve fine stones, beads, metal edges, texture, hooks, posts, and structural details.
• High-resolution output.
• Preferred e-commerce aspect ratio: 4:5.
• Square 1:1 may be used only where the downstream marketplace requires it.
• Maintain sufficient resolution for close inspection and product-detail verification.

IMPORTANT: Do not assume that requesting "4K" or "8K" in a prompt guarantees that resolution. The implementation must preserve the provider's actual supported output resolution.
Do not add fake resolution metadata.
Do not upscale solely to claim 4K/8K unless the existing pipeline already supports a legitimate image upscaling stage.

NEGATIVE / FAILURE PREVENTION — DO NOT generate:
• redesigned jewellery
• altered jewellery proportions
• different stones
• missing beads
• extra beads
• changed metal color
• changed clasp
• changed hook
• changed pin/post
• duplicate earrings
• extra jewellery
• two hands
• male/rough hand
• rough skin
• excessive veins
• rings or bracelets
• piercing
• earring post entering skin
• pin penetrating palm
• earring embedded in skin
• deformed hoop
• stretched jewellery
• resized jewellery
• floating jewellery
• unrealistic hand anatomy
• off-white background
• grey background
• cream background
• decorative props
• text
• labels
• measurements
• watermarks
• logos

FINAL VERIFICATION — Before producing the final image, internally verify:

1. Is there exactly ONE hand?
2. Does the hand look clean, soft, feminine, and manicured?
3. Is the earring the EXACT same product as the reference?
4. Are all stones, beads, metal components, hooks, posts, and structural details preserved?
5. Has the earring's original size/proportion remained unchanged?
6. If resting on the palm, is EVERY part of the earring above the skin?
7. Is NO pin/post/hook penetrating or appearing to pierce the skin?
8. Is the background truly #FFFFFF rather than near-white?
9. Are there absolutely NO additional props?
10. Does the image clearly communicate the jewellery's real physical scale?

If any answer is NO, correct the composition before generating the final image.
````

### 2.4 Professional Studio

| | |
|---|---|
| File | `backend/app/services/earring_professional_shot_prompt.py` |
| Builder | `build_professional_shot_prompt()` (line 269) |
| Pack slot / key | 4 of 6 — `"Professional Studio"` / `prompt_professional` |
| Variant | `environment="minimalist"` (default; the organic/luxury variants are not used by the pack) |
| Length / md5 | 12375 chars / `10ea7afbd6c9f3d21530e91bef410385` |
| Constants used | `GENERIC_EARRING_PRESERVATION` (earring_ecommerce_prompt.py:79), `MATERIAL_FIDELITY_INSTRUCTION` (earring_ecommerce_prompt.py:88), `ANTI_SYMMETRY_INSTRUCTION` (earring_ecommerce_prompt.py:111), `COLOUR_LOCK_INSTRUCTION` (earring_ecommerce_prompt.py:200), `NEGATIVE_CONSTRAINTS` (earring_professional_shot_prompt.py:242) |

````text
TASK: Professional Commercial Earring Photography & Dynamic Environment Engine.

Environment: Minimalist Studio

Generate a professional commercial photograph of the exact earring from the uploaded reference image, placed in a premium professional studio/tabletop environment matching the selected archetype.
The result must look like high-end professional commercial jewellery photography.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRODUCT FIDELITY — HIGHEST PRIORITY (NON-NEGOTIABLE):
The uploaded reference image is the single source of truth for the jewellery.
Preserve the exact visible product characteristics:

Preserve EXACTLY:
• overall earring identity
• geometry
• shape
• proportions
• metal structure
• metal colour
• bezel geometry
• prong count
• prong placement
• gemstone count
• gemstone placement
• gemstone shape
• facet/cut characteristics
• visible construction details
• hooks/posts
• links and joints
• asymmetry
• existing physical characteristics

Zero intentional product redesign is allowed.

Do NOT:
• add gemstones
• remove gemstones
• move gemstones
• change gemstone cuts
• alter prongs
• change bezel geometry
• change metal colour
• change material
• change proportions
• beautify the jewellery
• make the jewellery more symmetrical
• invent missing product details
• replace the product with a similar jewellery design

If any detail is unclear in the reference, DO NOT invent a replacement detail.

ANTI-REDESIGN RULE (NON-NEGOTIABLE):
This is a PRODUCT PHOTOGRAPHY task, NOT a design task.
You are placing the EXACT uploaded earring into a professional studio/tabletop environment. You are NOT designing a new earring, creating an inspired variation, or improving a product.
The generated image must show the EXACT same product — same shape, same stones, same metal, same proportions, same craftsmanship, same asymmetry, same imperfections.
The camera perspective changes to a professional commercial product-photography view. The jewellery must not change.

CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):
The reference image's actual visible asymmetry IS part of the product identity. If the reference shows asymmetry, the output MUST preserve that asymmetry exactly.

DO NOT:
• Make both sides identical simply because symmetry looks more aesthetically pleasing.
• Normalise geometry — do not straighten curves, regularise shapes, or correct perceived manufacturing imperfections.
• Beautify the product — do not smooth surfaces, round edges, or improve proportions beyond what the reference shows.
• Correct asymmetry — do not mirror one side to match the other.
• Adjust proportions — do not elongate, compress, or resize any element for visual balance.
• Invent details — do not add stones, engravings, filigree, or decorative elements not visible in the reference.
• Remove genuine details — do not remove elements that appear irregular, imperfect, or asymmetric.
• Convert an asymmetric product into a symmetric one.
• Treat visible irregularity as an error to be corrected.

The product in the reference is the ground truth. Any asymmetry, irregularity, or imperfection in the reference IS the product. Reproduce it faithfully.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.

EARRING TYPE: Preserve the exact earring type as shown in the reference. Do not convert one earring type into another (e.g. hoop to stud, stud to dangle, dangle to hoop). Preserve the complete structure including all attachment mechanisms, hanging elements, and connecting components.

MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):
The reference image is the authoritative source for all material appearance. Preserve EXACTLY as shown:
• Metal colour — do not convert silver to gold, gold to silver, brass to gold, or any other material substitution.
• Metal finish — preserve polished, brushed, matte, hammered, or any other surface treatment exactly.
• Plating appearance — preserve gold plating, silver plating, or any coating as shown.
• Gemstone colour — preserve every stone's exact colour without oversaturation or artificial brightening.
• Pearl appearance — preserve lustre, colour, and surface quality.
• Bead appearance — preserve colour, size, and arrangement.
• Reflectivity — preserve the natural reflectivity of the metal and stones.
Do NOT oversaturate colours. Do NOT artificially brighten the jewellery. A white background must NOT be interpreted as white jewellery. The material in the reference is the truth.

COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):
The reference image is the sole authority for the product's actual material and colour.
Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, tone-shift, or transform the product's metal or stone colours.
Silver must remain silver.
Gold must remain gold.
Gold plating must remain gold plating.
Silver plating must remain silver plating.
Platinum must remain platinum.
Brass must remain brass.
925 silver must remain 925 silver.
Blue stones must remain blue.
Red stones must remain red.
Green stones must remain green.
Clear stones must remain clear.
Do not infer a different material from studio lighting or reflections.
Lighting may change illumination ONLY.
Lighting must NEVER change the perceived underlying product material or colour.
The colour temperature of the output MUST match the reference. If the reference shows cool/silver tones, the output must NOT warm them to gold. If the reference shows warm tones, the output must NOT cool them to silver.

ENVIRONMENT & BACKDROP (NON-NEGOTIABLE):
Minimalist studio mount/stand environment.
Clean professional tabletop/studio environment.
Controlled neutral presentation.
Subtle grounding/contact shadow.

The jewellery rests naturally on a clean, premium, minimal surface. The environment must be visually neutral and unobtrusive — the jewellery is the sole focal point.

Do NOT generate:
• textured organic surfaces
• fabric or silk backgrounds
• busy or cluttered environments
• coloured or tinted backdrops
• plain pure white isolated catalog backgrounds

LIGHTING & SHADING (NON-NEGOTIABLE):
Soft controlled studio lighting appropriate to a minimalist studio environment.

• Balanced highlights across the jewellery.
• Clean metal-edge definition.
• Realistic contact shadow where jewellery meets the surface.
• Controlled gemstone brilliance — visible facet detail without blown highlights.
• Subtle environmental reflections on polished metal surfaces consistent with a clean studio setting.

Avoid:
• directional dramatic lighting
• high-contrast shadows
• warm/cool colour tinting
• excessive sparkle or artificial brilliance
• flat lighting that hides product detail
• unrealistic CGI reflections

Lighting must reveal the product without changing its appearance.

OPTICAL STYLE (NON-NEGOTIABLE):
Professional macro product photography.
Approximately 100mm macro visual perspective and f/4 depth-of-field appearance.
Keep the jewellery sharply resolved with controlled photographic depth of field.

Note: These values are visual photographic guidance, not guaranteed physical camera parameters. Treat them as direction for the visual aesthetic.

Maintain:
• clear gemstone detail
• clear prongs/settings
• readable metal edges
• realistic depth of field

Avoid:
• blurry gemstones
• excessive depth-of-field blur that hides the jewellery
• artificial sharpening that invents product detail

GEMSTONE PRESENTATION (NON-NEGOTIABLE):
Gemstones should have:
• clear detail
• realistic facet visibility
• controlled brilliance
• realistic optical response
• appropriate dispersion where naturally visible

Avoid:
• excessive artificial sparkle
• glowing gemstones
• blown highlights on gemstones
• fake gemstone geometry
• altered gemstone cuts

Visual enhancement must never change the actual product.

ENVIRONMENT INTERACTION (NON-NEGOTIABLE):
Allow the selected environment to influence realistic lighting, reflections, shadows, and visual context without changing the underlying jewellery geometry or product identity.

• Dynamic environment lighting/reflection should visually affect polished metal surfaces while preserving the underlying jewellery geometry.
• Represent environmental reflections as prompt-level photographic guidance — the image model should render realistic reflections consistent with the selected environment.
• The jewellery's metal surfaces should show subtle reflections consistent with the surrounding environment.

Do NOT:
• change jewellery geometry to match environment reflections
• add artificial ray-traced reflections
• override product identity with environment styling

GROUNDING & CONTACT SHADOWS (NON-NEGOTIABLE):
• Realistic grounding — the product must appear physically resting on or in contact with the selected environment surface.
• Natural contact shadows beneath the jewellery consistent with the surface type and lighting.
• Realistic ambient occlusion where the jewellery meets the surface.
• Product must appear physically present in the scene.

Avoid:
• floating jewellery
• disconnected shadows
• impossible grounding
• excessive shadows that obscure the product
• shadows inconsistent with the lighting direction

PRIMARY SUBJECT (NON-NEGOTIABLE):
The exact jewellery product remains the dominant subject.
The environment is a supporting element, not the primary focus.
The jewellery must be the sharpest and most visually prominent element in the composition.

NEGATIVE CONSTRAINTS (NON-NEGOTIABLE):
Prevent the following failure modes:
• on-ear, human model, earlobe, human ear
• skin texture, jawline, neck, portrait framing
• subsurface skin scattering, anatomical deformation
• distorted jewellery geometry
• altered product proportions
• altered prong count
• altered gemstone count
• altered gemstone cuts
• missing gemstones
• additional gemstones
• blurry gemstones
• floating product
• missing contact shadows
• unrealistic grounding
• artificial CGI plastic look
• flat lighting
• duplicated jewellery
• unrelated accessories
• redesigned jewellery

OUTPUT STYLE:
Professional commercial jewellery photography.
Premium studio/tabletop aesthetic.
Product detail-focused.
Editorial quality.

The jewellery is the primary visual subject.

IMAGE QUALITY:
• High-end professional macro jewellery photography.
• Extremely sharp jewellery details where in focus.
• Preserve fine stones, metal edges, texture, hooks, posts, and structural details.
• High-resolution output.
• Preferred aspect ratio: 4:5.

FINAL VERIFICATION — Before producing the final image, internally verify:

1. Is the jewellery the EXACT same product as the reference?
2. Are all gemstones, prongs, metal components, hooks, posts, and structural details preserved?
3. Has the jewellery's original size/proportion remained unchanged?
4. Is the environment consistent with the selected archetype?
5. Are there realistic contact shadows beneath the jewellery?
6. Is the jewellery the sharpest element in the image?
7. Is the environment subordinate to the jewellery?
8. Do metal surfaces show realistic environmental reflections?
9. Does the image look like professional commercial jewellery photography?
10. Is there absolutely NO human model, ear, skin, or anatomy visible?

If any answer is NO, correct the composition before generating the final image.
````

### 2.5 Lifestyle Shot

| | |
|---|---|
| File | `backend/app/services/earring_complementary_shot_prompt.py` |
| Builder | `build_complementary_shot_prompt()` (line 359) |
| Pack slot / key | 5 of 6 — `"Lifestyle Shot"` / `prompt_complementary` |
| Length / md5 | 14025 chars / `85cf2dd9d7e5679616cb0308778e335f` |
| Constants used | `PRODUCT_FIDELITY_ABSOLUTE` (earring_complementary_shot_prompt.py:59), `STAGING_INSTRUCTION` (earring_complementary_shot_prompt.py:96), `ARRANGEMENT_INSTRUCTION` (earring_complementary_shot_prompt.py:128), `PHYSICAL_CONTACT_INSTRUCTION` (earring_complementary_shot_prompt.py:153), `CAMERA_INSTRUCTION` (earring_complementary_shot_prompt.py:171), `LIGHTING_INSTRUCTION` (earring_complementary_shot_prompt.py:190), `BACKGROUND_COLOR_INSTRUCTION` (earring_complementary_shot_prompt.py:207), `VISUAL_HIERARCHY_INSTRUCTION` (earring_complementary_shot_prompt.py:231), `NEGATIVE_SPACE_INSTRUCTION` (earring_complementary_shot_prompt.py:250), `BEAD_CLUSTER_PRESERVATION_INSTRUCTION` (earring_complementary_shot_prompt.py:277), `STRICTLY_FORBIDDEN` (earring_complementary_shot_prompt.py:304), `QUALITY_CONTROL_CHECKLIST` (earring_complementary_shot_prompt.py:335), `GENERIC_EARRING_PRESERVATION` (earring_ecommerce_prompt.py:79), `MATERIAL_FIDELITY_INSTRUCTION` (earring_ecommerce_prompt.py:88), `ANTI_SYMMETRY_INSTRUCTION` (earring_ecommerce_prompt.py:111), `COLOUR_LOCK_INSTRUCTION` (earring_ecommerce_prompt.py:200) |

````text
TASK: Complementary Lifestyle Editorial Shot.
Generate a premium lifestyle editorial complementary image of the
EXACT jewellery shown in the reference image.

This is visually DISTINCT from the Professional Shot (Prompt 4).
While Prompt 4 uses a controlled studio environment with minimal
context, this shot uses asymmetric arrangement, premium contextual
surfaces, and storytelling composition to create a richer editorial
narrative.

The jewellery remains the dominant visual subject, but the
environment provides tasteful luxury context and depth.
This is NOT a plain hero/catalog shot and must NOT replicate a
standard e-commerce composition.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRODUCT FIDELITY — ABSOLUTE PRIORITY:
The reference image is the single source of truth for the jewellery.
Preserve the jewellery exactly as shown in the reference.

DO NOT:
• redesign the jewellery
• reinterpret the jewellery
• beautify by changing its structure
• add stones
• remove stones
• change stone count
• change stone shape
• change stone placement
• change bead count
• change bead arrangement
• change prongs
• change facets
• change clasp/lock
• change hooks
• change links
• change chains
• change metal structure
• change metal color
• change finish
• change proportions
• change thickness
• change geometry

Every visible jewellery component must correspond to the reference.
If a detail is unclear in the reference, DO NOT invent it.
The jewellery must remain the SAME physical product.

ANTI-REDESIGN RULE (NON-NEGOTIABLE):
This is a PRODUCT PHOTOGRAPHY task, NOT a design task.
You are photographing the EXACT uploaded product in a premium
editorial staging setting. You are NOT designing a new earring,
creating an inspired variation, or improving a product.
The generated image must show the EXACT same product — same shape,
same stones, same metal, same proportions, same craftsmanship,
same asymmetry, same imperfections.
Only the presentation changes: background surface, lighting,
composition, arrangement, and editorial quality.

CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):
The reference image's actual visible asymmetry IS part of the product identity. If the reference shows asymmetry, the output MUST preserve that asymmetry exactly.

DO NOT:
• Make both sides identical simply because symmetry looks more aesthetically pleasing.
• Normalise geometry — do not straighten curves, regularise shapes, or correct perceived manufacturing imperfections.
• Beautify the product — do not smooth surfaces, round edges, or improve proportions beyond what the reference shows.
• Correct asymmetry — do not mirror one side to match the other.
• Adjust proportions — do not elongate, compress, or resize any element for visual balance.
• Invent details — do not add stones, engravings, filigree, or decorative elements not visible in the reference.
• Remove genuine details — do not remove elements that appear irregular, imperfect, or asymmetric.
• Convert an asymmetric product into a symmetric one.
• Treat visible irregularity as an error to be corrected.

The product in the reference is the ground truth. Any asymmetry, irregularity, or imperfection in the reference IS the product. Reproduce it faithfully.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric,
  organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship
  between elements, and the relative size of every visible
  component.
• Stone arrangement: preserve every visible stone's position,
  spacing, grouping, and spatial relationship to other stones and
  to the metal structure.
• Stone placement: do not move stones from their visible
  locations.
• Stone characteristics: preserve the visible count, shapes,
  sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish,
  texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts,
  clasps, lever-backs, chains, or other attachment mechanisms
  exactly as shown.
• Decorative details: preserve all visible filigree, engravings,
  cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing,
  hammering, or any other surface treatment as shown.

EARRING TYPE: Preserve the exact earring type as shown in the reference. Do not convert one earring type into another (e.g. hoop to stud, stud to dangle, dangle to hoop). Preserve the complete structure including all attachment mechanisms, hanging elements, and connecting components.

MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):
The reference image is the authoritative source for all material appearance. Preserve EXACTLY as shown:
• Metal colour — do not convert silver to gold, gold to silver, brass to gold, or any other material substitution.
• Metal finish — preserve polished, brushed, matte, hammered, or any other surface treatment exactly.
• Plating appearance — preserve gold plating, silver plating, or any coating as shown.
• Gemstone colour — preserve every stone's exact colour without oversaturation or artificial brightening.
• Pearl appearance — preserve lustre, colour, and surface quality.
• Bead appearance — preserve colour, size, and arrangement.
• Reflectivity — preserve the natural reflectivity of the metal and stones.
Do NOT oversaturate colours. Do NOT artificially brighten the jewellery. A white background must NOT be interpreted as white jewellery. The material in the reference is the truth.

COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):
The reference image is the sole authority for the product's actual material and colour.
Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, tone-shift, or transform the product's metal or stone colours.
Silver must remain silver.
Gold must remain gold.
Gold plating must remain gold plating.
Silver plating must remain silver plating.
Platinum must remain platinum.
Brass must remain brass.
925 silver must remain 925 silver.
Blue stones must remain blue.
Red stones must remain red.
Green stones must remain green.
Clear stones must remain clear.
Do not infer a different material from studio lighting or reflections.
Lighting may change illumination ONLY.
Lighting must NEVER change the perceived underlying product material or colour.
The colour temperature of the output MUST match the reference. If the reference shows cool/silver tones, the output must NOT warm them to gold. If the reference shows warm tones, the output must NOT cool them to silver.

STAGING (NON-NEGOTIABLE):
Use a minimal premium editorial surface.

Allowed examples:
• neutral beige travertine
• matte stone
• plaster slab
• neutral geometric podium
• subtle ribbed architectural surface
• muted warm-grey or cream textured surface

The surface must remain secondary to the jewellery.

DO NOT use:
• plants
• flowers
• colored fabric
• decorative accessories
• jewellery boxes
• branded objects
• excessive ornaments
• busy backgrounds
• lifestyle scenes
• human models
• hands
• body parts

JEWELLERY ARRANGEMENT (NON-NEGOTIABLE):
Create an asymmetric editorial arrangement.

For a pair of earrings:
• One earring should rest naturally flat or at a slight angle on the
  elevated/riser surface.
• The second earring should be positioned slightly offset or leaning
  on the lower surface.
• The two pieces should not form a perfectly symmetrical hero
  arrangement.
• The arrangement must reveal depth, thickness, structure, and
  dimensionality.
• Both earrings must remain clearly visible.

The positioning must obey realistic physical gravity.
NO floating jewellery.
NO impossible intersections.
NO jewellery passing through the surface.
NO physically impossible orientation.

PHYSICAL CONTACT (NON-NEGOTIABLE):
Jewellery must appear physically present in the scene.

Use realistic:
• contact shadows
• grounding shadows
• occlusion
• weight
• surface contact
• reflections

Do not make the jewellery look pasted, cut out, or digitally floating.

CAMERA (NON-NEGOTIABLE):
Use an elevated three-quarter editorial perspective.
Preferred viewpoint: approximately 45-degree elevated angle or a
subtle diagonal top-down perspective.

The camera angle should reveal:
• jewellery depth
• thickness
• metal structure
• gemstone brilliance
• dimensional form

Avoid an extreme perspective that distorts the jewellery.

LIGHTING (NON-NEGOTIABLE):
Use soft directional studio lighting.

Lighting should produce:
• controlled metal highlights
• natural gemstone brilliance
• clean dimensional modelling
• delicate but defined shadows
• realistic surface reflections

Do not create exaggerated glow, artificial sparkle, or fantasy lighting.

BACKGROUND / COLOR (NON-NEGOTIABLE):
Use muted neutral tones only.

Preferred palette:
• soft beige
• warm grey
• cream
• muted stone
• neutral plaster

The background/surface must complement the jewellery without competing
with it.

If the jewellery contains colorful gemstones such as emeralds or
sapphires, the environment should remain sufficiently neutral for those
gemstones to remain visually prominent.

DO NOT use highly saturated backgrounds.

VISUAL HIERARCHY (NON-NEGOTIABLE):
Jewellery = PRIMARY SUBJECT.
Surface and staging elements = SECONDARY.

The jewellery should immediately attract the viewer's attention.
The environment must support the product rather than become the subject.

Target visual hierarchy:
approximately 90% jewellery focus,
approximately 10% environment/staging support.

This is a visual-priority rule, NOT permission to enlarge or distort
the jewellery.

STORYTELLING COMPOSITION (NON-NEGOTIABLE):
This is a lifestyle editorial shot, NOT a controlled studio product
shot. The composition should feel natural and aspirational — as if
the jewellery belongs in this premium environment.

Composition principles:
• Asymmetric, dynamic arrangement — not perfectly centered.
• Natural visual flow that guides the eye to the jewellery.
• Environmental context that enhances the product narrative.
• Subtle depth and layering in the scene.
• The environment tells a story about the product's premium
  positioning and target audience.

The jewellery must remain the clear primary focal point.
The environment supports the narrative without overwhelming.

NEGATIVE SPACE — HARD REQUIREMENT (NON-NEGOTIABLE):
The reference jewellery contains intentional OPEN / EMPTY areas.
These are structural features, not gaps to fill.

You MUST preserve every open/hollow region:
• Hollow crescent or open wireframe structures must remain hollow.
• Open centres of circular or geometric frames must remain empty.
• Spaces between hanging elements must remain separate.
• Gaps between bead clusters must remain visible.
• Openwork, filigree, or cut-out patterns must stay open.

DO NOT fill any internal void with:
• Gold or metal material
• Skin or flesh tone
• Background colour
• Gemstone material
• Decorative texture or pattern
• Shadow or shading

The empty space IS part of the jewellery geometry.
Filling it changes the product identity.

BEAD AND PEARL CLUSTER PRESERVATION (NON-NEGOTIABLE):
If the reference jewellery contains bead clusters, pearl groups,
or dangling bead arrangements:

• Each individual bead/pearl must remain visually
  distinguishable — no merging, fusing, or melting.
• Bead clusters must retain their individual separation — gaps
  between beads are part of the design.
• Large faceted teardrop drops must remain individually
  identifiable — each drop is a separate component.
• Hanging bead arrangements must preserve the exact count and
  relative positioning.
• Bead sizes must match the reference — do not enlarge small
  beads or shrink large ones.

DO NOT allow:
• Fused beads that merge into a single mass
• Melted or blob-like bead clusters
• Missing drops that were present in the reference
• Invented drops that were not in the reference
• Random bead blobs replacing structured clusters

STRICTLY FORBIDDEN — DO NOT generate:
• No redesign
• No additional jewellery
• No additional stones
• No missing stones
• No fake gemstones
• No altered prongs
• No altered clasp
• No altered metal structure
• No artificial jewellery extensions
• No floating pieces
• No impossible physics
• No humans
• No hands
• No models
• No plants
• No flowers
• No fabric
• No jewellery boxes
• No logos
• No text
• No watermark
• No brand elements
• No clutter
• No busy lifestyle environment

OUTPUT STYLE:
Premium lifestyle editorial jewellery photography.
Aspirational.
Sophisticated.
Product-focused with premium environmental context.

The jewellery is the hero of the composition.
The premium contextual surface provides tasteful context, depth,
and lifestyle narrative.

IMAGE QUALITY:
• Photorealistic lifestyle editorial photography.
• Extremely sharp jewellery details.
• Preserve fine stones, beads, metal edges, texture, hooks,
  posts, and structural details.
• Controlled depth of field — jewellery sharp, background
  slightly softer.
• High-resolution output.
• Preferred e-commerce aspect ratio: 4:5.
• Maintain sufficient resolution for close inspection and
  product-detail verification.

QUALITY CONTROL — Before accepting the generated result, verify:

1. Product identity matches the reference.
2. Stone count and placement are preserved.
3. Prongs and settings are preserved.
4. Beads/components are preserved.
5. Clasp/lock/hook structure is preserved.
6. Metal color and structure are preserved.
7. Jewellery proportions are preserved.
8. Both pieces are physically grounded.
9. Contact shadows look realistic.
10. No piece is floating.
11. No AI-generated jewellery components have been introduced.
12. Background remains neutral and secondary.
13. Jewellery remains the dominant visual subject.
14. No human, decorative, branded, or unrelated object appears.

If any answer is FAIL, correct the composition before generating the
final image.
````

### 2.6 UGC Style

| | |
|---|---|
| File | `backend/app/services/earring_ugc_style_prompt.py` |
| Builder | `build_ugc_style_prompt()` (line 52) |
| Pack slot / key | 6 of 6 — `"UGC Style"` / `prompt_ugc` |
| Length / md5 | 12216 chars / `266ecd42aaaf0fe3ef7b1858386ba4a0` |
| Constants used | `GENERIC_EARRING_PRESERVATION` (earring_ecommerce_prompt.py:79), `MATERIAL_FIDELITY_INSTRUCTION` (earring_ecommerce_prompt.py:88), `ANTI_SYMMETRY_INSTRUCTION` (earring_ecommerce_prompt.py:111), `COLOUR_LOCK_INSTRUCTION` (earring_ecommerce_prompt.py:200), `NEGATIVE_SPACE_INSTRUCTION` (earring_scale_reference_prompt.py:200), `BEAD_CLUSTER_PRESERVATION_INSTRUCTION` (earring_scale_reference_prompt.py:227) |

````text
CRITICAL: The earrings in the output MUST be an exact 1:1 physical replica of the earrings provided in the reference image. Retain exact stone count, stone shapes (e.g., baguette, marquise, pear, round), prong setting structure, metal tone, and earring silhouette. DO NOT alter the core jewelry design or invent alternate motifs.

Remove all retail packaging, polybags, display cards, plastic film, and human fingers. Extract the jewelry piece with pristine studio fidelity.

TASK: UGC Style.
Create an authentic customer-perspective UGC lifestyle photograph of the exact jewellery from the provided reference image.
This is a user-generated content style image — it should look like a genuine customer photograph, not a clinical studio shot.

REFERENCE IMAGE PRIORITY: MAXIMUM

PRODUCT FIDELITY — ABSOLUTE PRIORITY:
The uploaded reference image is the single source of truth for the jewellery.
Preserve the original product exactly.

Preserve:
- exact product geometry
- exact silhouette
- gemstone count
- gemstone placement
- gemstone cuts and facet structure
- prong settings
- micro-pave details
- bead arrangement
- hanging components
- hooks and connectors
- metal structure
- silver/white-gold appearance
- original proportions
- distinctive product details

Do NOT:
- redesign the jewellery
- reconstruct into a similar product
- add stones
- remove stones
- duplicate stones
- merge components
- change metal color
- change geometry
- beautify into a different design
- invent missing details
- simplify detailed components

Every visible stone, bead, prong, connector, clasp, hook, link, and structural element must correspond to the reference image.
If any detail is unclear in the reference, DO NOT invent a replacement detail.

ANTI-REDESIGN RULE (NON-NEGOTIABLE):
This is a PRODUCT PHOTOGRAPHY task, NOT a design task.
You are creating an authentic customer photograph of the EXACT uploaded product. You are NOT designing a new earring, creating an inspired variation, or improving a product.
The generated image must show the EXACT same product — same shape, same stones, same metal, same proportions, same craftsmanship, same asymmetry, same imperfections.
The scene and environment change. The jewellery must not.

CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):
The reference image's actual visible asymmetry IS part of the product identity. If the reference shows asymmetry, the output MUST preserve that asymmetry exactly.

DO NOT:
• Make both sides identical simply because symmetry looks more aesthetically pleasing.
• Normalise geometry — do not straighten curves, regularise shapes, or correct perceived manufacturing imperfections.
• Beautify the product — do not smooth surfaces, round edges, or improve proportions beyond what the reference shows.
• Correct asymmetry — do not mirror one side to match the other.
• Adjust proportions — do not elongate, compress, or resize any element for visual balance.
• Invent details — do not add stones, engravings, filigree, or decorative elements not visible in the reference.
• Remove genuine details — do not remove elements that appear irregular, imperfect, or asymmetric.
• Convert an asymmetric product into a symmetric one.
• Treat visible irregularity as an error to be corrected.

The product in the reference is the ground truth. Any asymmetry, irregularity, or imperfection in the reference IS the product. Reproduce it faithfully.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.

EARRING TYPE: Preserve the exact earring type as shown in the reference. Do not convert one earring type into another (e.g. hoop to stud, stud to dangle, dangle to hoop). Preserve the complete structure including all attachment mechanisms, hanging elements, and connecting components.

MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):
The reference image is the authoritative source for all material appearance. Preserve EXACTLY as shown:
• Metal colour — do not convert silver to gold, gold to silver, brass to gold, or any other material substitution.
• Metal finish — preserve polished, brushed, matte, hammered, or any other surface treatment exactly.
• Plating appearance — preserve gold plating, silver plating, or any coating as shown.
• Gemstone colour — preserve every stone's exact colour without oversaturation or artificial brightening.
• Pearl appearance — preserve lustre, colour, and surface quality.
• Bead appearance — preserve colour, size, and arrangement.
• Reflectivity — preserve the natural reflectivity of the metal and stones.
Do NOT oversaturate colours. Do NOT artificially brighten the jewellery. A white background must NOT be interpreted as white jewellery. The material in the reference is the truth.

COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):
The reference image is the sole authority for the product's actual material and colour.
Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, tone-shift, or transform the product's metal or stone colours.
Silver must remain silver.
Gold must remain gold.
Gold plating must remain gold plating.
Silver plating must remain silver plating.
Platinum must remain platinum.
Brass must remain brass.
925 silver must remain 925 silver.
Blue stones must remain blue.
Red stones must remain red.
Green stones must remain green.
Clear stones must remain clear.
Do not infer a different material from studio lighting or reflections.
Lighting may change illumination ONLY.
Lighting must NEVER change the perceived underlying product material or colour.
The colour temperature of the output MUST match the reference. If the reference shows cool/silver tones, the output must NOT warm them to gold. If the reference shows warm tones, the output must NOT cool them to silver.

SIZE & PROPORTION (NON-NEGOTIABLE):
Maintain the jewellery's original proportions from the reference.
Do not make the jewellery unnaturally large, small, elongated, compressed, widened, thickened, or otherwise distorted.
The output should look like the SAME physical jewellery photographed by a customer in a natural setting.

Preserve relative dimensions between every component.
Do not exaggerate or minimize any element for visual impact.
PRODUCT FIDELITY HAS PRIORITY OVER VISUAL STYLING.

NEGATIVE SPACE — HARD REQUIREMENT (NON-NEGOTIABLE):
The reference jewellery contains intentional OPEN / EMPTY areas. These are structural features, not gaps to fill.

You MUST preserve every open/hollow region:
• Hollow crescent or open wireframe structures must remain hollow.
• Open centres of circular or geometric frames must remain empty.
• Spaces between hanging elements must remain separate.
• Gaps between bead clusters must remain visible.
• Openwork, filigree, or cut-out patterns must stay open.

DO NOT fill any internal void with:
• Gold or metal material
• Skin or flesh tone
• Background colour
• Gemstone material
• Decorative texture or pattern
• Shadow or shading

The empty space IS part of the jewellery geometry. Filling it changes the product identity.

BEAD AND PEARL CLUSTER PRESERVATION (NON-NEGOTIABLE):
If the reference jewellery contains bead clusters, pearl groups, or dangling bead arrangements:

• Each individual bead/pearl must remain visually distinguishable — no merging, fusing, or melting.
• Bead clusters must retain their individual separation — gaps between beads are part of the design.
• Large faceted teardrop drops must remain individually identifiable — each drop is a separate component.
• Hanging bead arrangements must preserve the exact count and relative positioning.
• Bead sizes must match the reference — do not enlarge small beads or shrink large ones.

DO NOT allow:
• Fused beads that merge into a single mass
• Melted or blob-like bead clusters
• Missing drops that were present in the reference
• Invented drops that were not in the reference
• Random bead blobs replacing structured clusters

UGC ENVIRONMENT (NON-NEGOTIABLE):
Place the jewellery naturally in a believable customer setting.

Allowed environments include:
• A vanity tray with a minimal perfume bottle or silk pouch
• An open jewellery box during an unboxing moment
• A wooden desk
• Textured linen surface

The setting should feel like a real customer's space — personal, tasteful, and lived-in.

The jewellery should be naturally placed or laid, showing realistic physical contact with the surface.

The jewellery must remain the main sharp focal point.

Do NOT generate:
• Pure flat white catalog background
• White isolated e-commerce presentation
• Plain empty canvas
• Clinical studio environment
• CGI render aesthetic

LIGHTING & CAMERA (NON-NEGOTIABLE):
Use soft natural window daylight.

• Realistic shadows — not harsh flash.
• Natural depth of field — gentle background blur.
• Authentic high-end smartphone photography look.
• Controlled gemstone reflections.
• Realistic metallic highlights.
• Visible stone facets where light catches them.

The image should feel like a genuine customer photograph, not a clinical studio product shot or CGI render.

Lighting must reveal the product. Lighting must NOT beautify, recolour, redesign, or alter the jewellery.
No excessive retouching.
No unrealistic reflections.
No distorted gemstones.
No warped prongs.

STRICTLY FORBIDDEN — DO NOT generate:
• No human model
• No face
• No hand
• No fingers
• No body parts
• No clothing
• No pure flat white catalog background
• No white isolated e-commerce presentation
• No plain empty canvas
• No harsh flash
• No excessive retouching
• No unrealistic reflections
• No distorted gemstones
• No warped prongs
• No altered metal color
• No redesigned jewellery
• No altered jewellery proportions
• No different stones
• No missing beads
• No extra beads
• No changed clasp
• No changed hook
• No changed pin/post
• No deformed geometry
• No stretched jewellery
• No resized jewellery
• No floating jewellery
• No text
• No logo
• No watermark

OUTPUT STYLE:
Authentic customer-perspective UGC photography.
Natural.
Lifestyle.
Believable.
Product-focused.

The product itself must remain the sharpest element.

IMAGE QUALITY:
• High-end smartphone photography aesthetic.
• Extremely sharp jewellery details.
• Preserve fine stones, beads, metal edges, texture, hooks, posts, and structural details.
• High-resolution output.
• Preferred aspect ratio: 4:5.

FINAL VERIFICATION — Before producing the final image, internally verify:

1. Is the jewellery the EXACT same product as the reference?
2. Are all stones, beads, metal components, hooks, posts, clasps, and structural details preserved?
3. Has the jewellery's original size/proportion remained unchanged?
4. Is there absolutely NO human model, face, hand, or body part?
5. Is the setting a believable customer environment (not studio)?
6. Is there NO pure white background or isolated white canvas?
7. Does the lighting look like natural window daylight?
8. Is the jewellery the main sharp focal point?
9. Does the image feel like a genuine customer photograph?
10. Are there no unrealistic reflections or distorted gemstones?

If any answer is NO, correct the composition before generating the final image.
````

## 3. Appendix: provider anchors

### REFERENCE_PRIORITY_BLOCK

`backend/app/ai/product_fidelity.py` — appended by `ImageGenerationManager` only when a prompt lacks the marker; none of the live prompts lack it, and the Clean Studio prompt embeds it

````text
REFERENCE IMAGE PRIORITY: MAXIMUM

PRIORITY RULE: REFERENCE PRODUCT IDENTITY > SCENE / CREATIVE INSTRUCTION.
The uploaded image is the single authoritative source of product truth.  The text prompt may describe the desired scene, presentation, or styling — but it must never override, reinterpret, or replace the physical identity of the product shown in the reference image.

PRODUCT IDENTITY — PRESERVE EXACTLY:
• Overall silhouette and outline shape.
• Geometry: the exact form (circular, teardrop, geometric, organic, or any other visible shape).
• Proportions: the exact length-to-width ratio, the relationship between elements, and the relative size of every visible component.
• Stone arrangement: preserve every visible stone's position, spacing, grouping, and spatial relationship to other stones and to the metal structure.
• Stone placement: do not move stones from their visible locations.
• Stone characteristics: preserve the visible count, shapes, sizes, colours, and relative prominence of every stone as shown.
• Metal appearance: preserve the exact metal colour, finish, texture, and surface quality as shown in the reference.
• Attachment structure: preserve any visible hooks, posts, clasps, lever-backs, chains, or other attachment mechanisms exactly as shown.
• Decorative details: preserve all visible filigree, engravings, cut-outs, milgrain, surface patterns, and ornamental elements.
• Surface details: preserve visible texture, polish, brushing, hammering, or any other surface treatment as shown.
• Symmetry or asymmetry: preserve the exact visible symmetry — do not symmetrise an asymmetric design or vice versa.

WHAT YOU MUST NOT CHANGE:
• Do not redesign or reinterpret the product.
• Do not simplify or abstract the design.
• Do not add stones, decorative elements, or features not shown.
• Do not remove stones, decorative elements, or features that are shown.
• Do not change stone arrangement, spacing, or placement.
• Do not change proportions or geometry.
• Do not change metal colour, finish, or texture.
• Do not change attachment structure or type.
• Do not change the product's visible size relative to the reference — do not enlarge or shrink it.
• Do not turn a single piece into a pair or a pair into a single piece.
• Do not create a 'similar' or 'inspired' variation.

WHAT YOU MAY CHANGE (PRESENTATION ONLY):
• Background and environment.
• Lighting quality, direction, and colour temperature.
• Camera angle, focal length, and depth of field.
• Composition and framing.
• Sharpness, resolution, and commercial presentation quality.

This is professional product photography of the exact uploaded product — not creative generation, not a redesign, and not a new interpretation.
````

### REFERENCE_IMAGE_ANCHOR

`backend/app/ai/product_fidelity.py` — Gemini: sent as the first content part, before the reference image

````text
PRODUCT IDENTITY LOCK: The attached image is the EXACT product to photograph. Preserve the jewellery EXACTLY as shown in it — same design, shape, gemstone placement and count, metal colour, texture, and proportions. Do not redesign, replace, or invent any part of the jewellery. Only change the background, environment, camera angle, composition, lighting, and styling. Treat this as professional product photography of the attached piece — never a creative reimagining.
````

### OPENAI_IDENTITY_ANCHOR

`backend/app/ai/providers/openai_image_provider.py` — OpenAI fallback: prepended to the prompt for images.edit

````text
The uploaded reference image is the authoritative source for the product's identity.
Preserve the exact product shown in the reference image.
Do not redesign, reinterpret, simplify, add, remove, or substitute product features.
Preserve all visible product-specific details, including:
- overall design and geometry
- proportions
- stone arrangement and placement
- metal appearance
- attachment structure
- decorative details
Only apply the requested scene or presentation changes.
If the scene instruction conflicts with the reference product's physical identity, preserve the reference product.
````
