# Local font assets

LXGW WenKai Lite is vendored under `lxgw-wenkai-lite/`; the application never contacts a font CDN.

| Consumer | CSS | Font asset |
| --- | --- | --- |
| Browser | `lxgwwenkailite-regular.css` | Unicode-range WOFF2 files in `files/` |
| PDF export | `lxgwwenkailite-regular-pdf.css` | `files/lxgwwenkailite-regular-full.woff2` |

The browser keeps its Unicode-range subsets for efficient page loading. PDF export deliberately uses the one complete WOFF2, with no `unicode-range`, because WeasyPrint can mis-map CJK characters when it processes the many browser subsets under the same font family.

The PDF WOFF2 was converted with FontTools, without subsetting, from the official [LXGW WenKai Lite v1.250 release](https://github.com/lxgw/LxgwWenKai-Lite/releases/tag/v1.250), specifically `LXGWWenKaiLite-Regular.ttf`. Its version matches `lxgw-wenkai-lite/VERSION`; its SHA-256 is `f61732c809e152eae3b4a6160c1dd5e11eb2a12356e3cf56901f506ccd9992ec`.

The font software is distributed under SIL Open Font License 1.1; see `lxgw-wenkai-lite/OFL.txt`. `lxgw-wenkai-lite/LICENSE` is retained with the browser subset package.
