# Prefrontal — brand mark

A hand-authored vector interpretation of the Prefrontal icon: a single
continuous stroke that reads at once as a **P** (Prefrontal), a **cortex in
profile** (the two curls are brain folds), and — because it's drawn as one
unbroken line — the product idea of a single connected system.

## Files

| File | What it is |
|---|---|
| `prefrontal-app-icon.svg` | Primary mark on the rounded-square (app-icon) field. **Source of truth.** |
| `prefrontal-favicon.svg` | Same mark on a round field, for favicons / avatars. **Source of truth.** |
| `favicon.ico` | Multi-size ICO (16/32/48/256) packed from the round mark. |
| `png/prefrontal-app-icon-{16…1024}.png` | Rasterized app-icon ladder. |
| `png/favicon-{16,32,48,256}.png` | Rasterized round-mark ladder. |

The two SVGs are the only things to edit by hand; every PNG and the `.ico` are
generated from them (see **Regenerating** below).

## Palette

| Token | Value | Use |
|---|---|---|
| Field navy | `#141b30` | Background. Matches the `tech-box` in `docs/one-sheet` / `docs/parent-pack`. |
| Gradient (top→bottom) | `#c6ec4e` → `#66db6e` → `#2dd4bf` → `#38bdf8` | Lime → green → teal → cyan, along the stroke. |

Stroke: width `60` on a `1024` viewBox (~5.9%), round caps and joins — the
rounding is load-bearing for the soft, continuous feel and for legibility when
the mark is scaled down.

> Note: the collateral (`docs/one-sheet`, `docs/parent-pack`) currently leads
> with indigo `#4338ca` as the brand accent, which the mark does not use.
> Aligning the two palettes (indigo vs. this navy + green→cyan) is an open brand
> decision, deliberately left untouched here.

## Regenerating the rasters

The SVGs render at their intrinsic `1024×1024`; small window sizes clip rather
than scale in headless Chromium, so the ladder is produced by rendering the two
masters at 1024 and box-downscaling (dependency-free, straight-alpha aware):

```
# 1024 masters (Chromium headless):
chrome --headless=new --no-sandbox --window-size=1024,1024 \
  --default-background-color=00000000 \
  --screenshot=png/src-app-1024.png file://$PWD/prefrontal-app-icon.svg

# then downscale + pack the .ico with the scratch pngtool.py
# (pure-Python PNG box-downscale + PNG-in-ICO packer)
```
