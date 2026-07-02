# Prefrontal — brand mark

The Prefrontal icon: a single continuous stroke that reads at once as a **P**
(Prefrontal), a **cortex in profile** (the bowl is a two-gyrus brain), and —
because it's drawn as one unbroken line — the product idea of a single connected
system.

## Files

| File | What it is |
|---|---|
| `prefrontal-app-icon.svg` | Primary mark on the rounded-square (app-icon) field. **Source of truth.** |
| `prefrontal-favicon.svg` | Same mark on a round field, for favicons / avatars. **Source of truth.** |
| `prefrontal-lockup.svg` | Horizontal lockup — icon + "Prefrontal" wordmark (Space Grotesk, embedded). **Recommended lockup.** |
| `favicon.ico` | Multi-size ICO (16/32/48/256) packed from the round mark. |
| `png/prefrontal-app-icon-{16…1024}.png` | Rasterized app-icon ladder. |
| `png/favicon-{16,32,48,256}.png` | Rasterized round-mark ladder. |

The three SVGs are the only things to edit by hand; every PNG and the `.ico` are
generated from them (see **Regenerating** below).

## Palette

| Token | Value | Use |
|---|---|---|
| Field navy | `#061A3D` | Icon background; dark surfaces. |
| Lime | `#A7F07A` | Gradient stop 0 (top of the stroke). |
| Teal / mint | `#43E6C2` | Gradient stop ~0.55. |
| Blue | `#2AA7FF` | Gradient stop 1 (bottom of the stroke). |
| Near-white | `#F8FAFC` | Wordmark / mark on dark surfaces. |

Stroke: width `66` on a `1024` viewBox (~6.4%), round caps and joins — the
rounding is load-bearing for the soft, continuous feel and for legibility when
the mark is scaled down. The stroke gradient runs lime → teal → blue,
top-to-bottom.

**Wordmark:** Space Grotesk (weight 600 for the lockup). It's
[SIL Open Font License](https://fonts.google.com/specimen/Space+Grotesk); the
latin subset is embedded as a base64 `@font-face` in `prefrontal-lockup.svg` and
in the doc headers so the PDFs render without a network fetch.

### How it's applied in the docs

The one-sheet and parent-pack headers now carry the icon + a Space Grotesk
wordmark. Per a deliberate "keep the existing palette" call, those sheets still
use their **semantic** colors — green = *today*, amber = *roadmap*, indigo =
brand/tech — so the wordmark there stays indigo rather than navy. The **mark
itself** uses the official navy + green→blue everywhere. A full realign of the
collateral to the navy + green→cyan system (retiring indigo) is a possible
future pass, not done here.

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
