"""Draw the 탈것 sticker pack in the same style as static/chat-emoji/office.

Shared vocabulary with that pack: one dark rounded outline, flat fills with a
soft top-light gradient, and a kawaii face (glossy eyes, pink blush, open smile).
Rendered to 320x320 PNG with a transparent background. This is a build-time
tool, not part of the app: it needs `pip install resvg-py`, which the server
never imports.

    python scripts/draw_vehicle_stickers.py static/chat-emoji/vehicle [이름 ...]
"""
import sys
from pathlib import Path

import resvg_py

SIZE = 320
INK = '#3a3340'          # outline
SW = 7                   # outline width for body shapes
EYE = '#3a3340'
BLUSH = '#ff8fa8'
TONGUE = '#ff7d90'

GLASS = ('#dff2ff', '#a9d8f5')
WHITE = ('#ffffff', '#e6e9f2')
RED = ('#ff7a6d', '#e8483d')
YELLOW = ('#ffd980', '#f5b731')
BLUE = ('#7fb6ff', '#3f7fe8')
MINT = ('#7fe3d8', '#2fb9ab')
ORANGE = ('#ffb066', '#f0812e')
GREY = ('#f2f4fa', '#cdd3e2')
DARK = ('#59525f', '#38323f')
WOOD = ('#f0a166', '#d3702f')

_defs = []


def grad(light, base, vertical=True):
    """Register a top-light linear gradient and return its url()."""
    name = f'g{len(_defs)}'
    x2, y2 = (0, 1) if vertical else (1, 1)
    _defs.append(
        f'<linearGradient id="{name}" x1="0" y1="0" x2="{x2}" y2="{y2}">'
        f'<stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{base}"/>'
        f'</linearGradient>'
    )
    return f'url(#{name})'


def eyes(cx, cy, gap=42, rx=13, ry=17):
    out = []
    for x in (cx - gap / 2, cx + gap / 2):
        out.append(f'<ellipse cx="{x}" cy="{cy}" rx="{rx}" ry="{ry}" fill="{EYE}" stroke="none"/>')
        out.append(f'<ellipse cx="{x - rx * .32}" cy="{cy - ry * .34}" rx="{rx * .34}" ry="{ry * .3}" '
                   f'fill="#fff" stroke="none"/>')
        out.append(f'<circle cx="{x + rx * .36}" cy="{cy + ry * .38}" r="{rx * .18}" '
                   f'fill="#fff" opacity=".8" stroke="none"/>')
    return ''.join(out)


def smile(cx, cy, w=24, h=20):
    name = f'm{len(_defs)}'
    path = f'M {cx - w},{cy} A {w},{h} 0 0 0 {cx + w},{cy} Z'
    _defs.append(f'<clipPath id="{name}"><path d="{path}"/></clipPath>')
    return (f'<path d="{path}" fill="{EYE}" stroke="none"/>'
            f'<ellipse cx="{cx}" cy="{cy + h * .95}" rx="{w * .52}" ry="{h * .55}" '
            f'fill="{TONGUE}" clip-path="url(#{name})" stroke="none"/>')


def blush(cx, cy, gap, rx=14, ry=8):
    return ''.join(
        f'<ellipse cx="{cx + s * gap / 2}" cy="{cy}" rx="{rx}" ry="{ry}" '
        f'fill="{BLUSH}" opacity=".55" stroke="none"/>' for s in (-1, 1))


def face(cx, cy, gap=42, eye_ry=17, mouth_dy=30, mouth_w=24, blush_gap=104, blush_dy=22):
    """Eyes on the centre line, blush and an open smile below them."""
    return (blush(cx, cy + blush_dy, blush_gap)
            + eyes(cx, cy, gap, ry=eye_ry)
            + smile(cx, cy + mouth_dy, mouth_w, mouth_w * .82))


def gloss(d, opacity=.35):
    return f'<path d="{d}" fill="#fff" opacity="{opacity}" stroke="none"/>'


# --------------------------------------------------------------------------- #
# Each builder returns the body markup; the outline attributes live on the <g>.
# --------------------------------------------------------------------------- #

def car():
    return f'''
    <rect x="48" y="214" width="50" height="54" rx="20" fill="{grad(*DARK)}"/>
    <rect x="222" y="214" width="50" height="54" rx="20" fill="{grad(*DARK)}"/>
    <rect x="32" y="204" width="256" height="56" rx="26" fill="{grad(*GREY)}"/>
    <rect x="40" y="124" width="240" height="110" rx="44" fill="{grad(*RED)}"/>
    <rect x="72" y="50" width="176" height="104" rx="42" fill="{grad('#ff6f61', '#d43e34')}"/>
    <rect x="86" y="64" width="148" height="84" rx="28" fill="{grad(*GLASS)}"/>
    {gloss('M 100,72 L 140,72 L 108,140 L 92,140 Q 88,110 100,72 Z', .45)}
    <rect x="58" y="166" width="54" height="34" rx="16" fill="{grad(*YELLOW)}"/>
    <rect x="208" y="166" width="54" height="34" rx="16" fill="{grad(*YELLOW)}"/>
    <rect x="128" y="176" width="64" height="26" rx="12" fill="{grad(*DARK)}"/>
    <ellipse cx="34" cy="140" rx="14" ry="18" fill="{grad(*RED)}"/>
    <ellipse cx="286" cy="140" rx="14" ry="18" fill="{grad(*RED)}"/>
    {face(160, 100, gap=46, mouth_dy=30, mouth_w=22, blush_gap=112, blush_dy=22)}
    '''


def bus():
    return f'''
    <rect x="66" y="248" width="46" height="34" rx="14" fill="{grad(*DARK)}"/>
    <rect x="208" y="248" width="46" height="34" rx="14" fill="{grad(*DARK)}"/>
    <rect x="46" y="40" width="228" height="222" rx="32" fill="{grad(*YELLOW)}"/>
    <rect x="102" y="50" width="116" height="22" rx="10" fill="{grad(*DARK)}"/>
    <rect x="62" y="82" width="196" height="94" rx="26" fill="{grad(*GLASS)}"/>
    {gloss('M 80,90 L 118,90 L 84,168 L 70,168 Q 66,124 80,90 Z', .45)}
    <rect x="62" y="188" width="196" height="46" rx="18" fill="{grad(*WHITE)}"/>
    <circle cx="92" cy="211" r="17" fill="{grad(*YELLOW)}"/>
    <circle cx="228" cy="211" r="17" fill="{grad(*YELLOW)}"/>
    <rect x="130" y="196" width="60" height="30" rx="12" fill="{grad(*DARK)}"/>
    <rect x="40" y="238" width="240" height="30" rx="14" fill="{grad(*GREY)}"/>
    <ellipse cx="28" cy="106" rx="13" ry="17" fill="{grad(*DARK)}"/>
    <ellipse cx="292" cy="106" rx="13" ry="17" fill="{grad(*DARK)}"/>
    {face(160, 120, gap=52, mouth_dy=34, mouth_w=24, blush_gap=122, blush_dy=26)}
    '''


def bicycle():
    wheel = ('<circle cx="{cx}" cy="206" r="62" fill="{fill}"/>'
             '<circle cx="{cx}" cy="206" r="44" fill="none" stroke-width="5"/>')
    return f'''
    {wheel.format(cx=94, fill=grad(*WHITE))}
    <g stroke-width="4" opacity=".5">
      <line x1="94" y1="164" x2="94" y2="248"/><line x1="52" y1="206" x2="136" y2="206"/>
      <line x1="64" y1="176" x2="124" y2="236"/><line x1="64" y1="236" x2="124" y2="176"/>
    </g>
    <circle cx="94" cy="206" r="9" fill="{grad(*DARK)}"/>
    <g stroke-width="12" fill="none" stroke="{grad(*MINT)}" stroke-linecap="round">
      <path d="M 166,206 L 144,124"/><path d="M 166,206 L 212,132"/>
      <path d="M 144,124 L 212,132"/><path d="M 166,206 L 94,206"/>
      <path d="M 144,124 L 94,206"/><path d="M 212,132 L 230,206"/>
    </g>
    <path d="M 122,116 Q 148,106 168,118 Q 148,128 122,124 Z" fill="{grad(*DARK)}"/>
    <path d="M 196,116 L 240,112" stroke-width="10" stroke-linecap="round"/>
    <circle cx="166" cy="206" r="15" fill="{grad(*DARK)}"/>
    <rect x="150" y="228" width="30" height="12" rx="5" fill="{grad(*DARK)}"/>
    {wheel.format(cx=230, fill=grad(*WHITE))}
    {face(230, 196, gap=38, eye_ry=15, mouth_dy=28, mouth_w=19, blush_gap=88, blush_dy=20)}
    '''


def motorcycle():
    return f'''
    <rect x="42" y="192" width="70" height="22" rx="11" fill="{grad(*GREY)}"/>
    <circle cx="82" cy="222" r="44" fill="{grad(*DARK)}"/>
    <circle cx="82" cy="222" r="19" fill="{grad(*GREY)}" stroke-width="5"/>
    <circle cx="244" cy="222" r="44" fill="{grad(*DARK)}"/>
    <circle cx="244" cy="222" r="19" fill="{grad(*GREY)}" stroke-width="5"/>
    <path d="M 228,122 L 244,222" stroke-width="13" stroke-linecap="round"/>
    <path d="M 202,112 L 256,104" stroke-width="11" stroke-linecap="round"/>
    <circle cx="260" cy="108" r="9" fill="{grad(*DARK)}"/>
    <path d="M 84,158 L 128,150 Q 146,148 150,164 L 92,174 Z" fill="{grad(*DARK)}"/>
    <path d="M 208,140 Q 238,142 242,172 L 210,180 Z" fill="{grad(*ORANGE)}"/>
    <rect x="98" y="128" width="126" height="66" rx="30" fill="{grad(*ORANGE)}"/>
    <circle cx="240" cy="156" r="16" fill="{grad(*YELLOW)}"/>
    {gloss('M 116,142 Q 160,132 202,140 L 198,152 Q 158,144 120,156 Z', .4)}
    {face(160, 152, gap=38, eye_ry=15, mouth_dy=28, mouth_w=19, blush_gap=92, blush_dy=20)}
    '''


def ship():
    return f'''
    <rect x="152" y="70" width="46" height="62" rx="12" fill="{grad(*YELLOW)}"/>
    <rect x="152" y="86" width="46" height="18" fill="{grad(*RED)}" stroke="none"/>
    <path d="M 152,86 L 198,86 M 152,104 L 198,104" stroke-width="5"/>
    <rect x="94" y="124" width="140" height="72" rx="18" fill="{grad(*WHITE)}"/>
    <path d="M 34,190 L 288,190 L 264,250 Q 256,266 236,266 L 82,266 Q 58,266 46,240 Z"
          fill="{grad(*RED)}"/>
    <path d="M 40,214 L 278,214" stroke-width="6" opacity=".55"/>
    <circle cx="84" cy="236" r="13" fill="{grad(*GLASS)}"/>
    <circle cx="126" cy="240" r="13" fill="{grad(*GLASS)}"/>
    <circle cx="236" cy="236" r="13" fill="{grad(*GLASS)}"/>
    {gloss('M 106,134 L 130,134 L 112,190 L 100,190 Q 96,152 106,134 Z', .5)}
    {face(164, 150, gap=42, eye_ry=16, mouth_dy=30, mouth_w=22, blush_gap=104, blush_dy=22)}
    '''


def airplane():
    return f'''
    <path d="M 62,132 L 30,44 L 74,44 L 100,132 Z" fill="{grad(*BLUE)}"/>
    <path d="M 150,180 L 92,252 L 140,252 L 190,186 Z" fill="{grad(*BLUE)}"/>
    <rect x="34" y="118" width="256" height="78" rx="39" fill="{grad(*WHITE)}"/>
    <path d="M 40,178 L 198,174" stroke-width="6" stroke="{grad(*BLUE)}" opacity=".9"/>
    <rect x="116" y="200" width="66" height="34" rx="16" fill="{grad(*GREY)}"/>
    <circle cx="86" cy="140" r="10" fill="{grad(*GLASS)}" stroke-width="4"/>
    <circle cx="118" cy="140" r="10" fill="{grad(*GLASS)}" stroke-width="4"/>
    <circle cx="150" cy="140" r="10" fill="{grad(*GLASS)}" stroke-width="4"/>
    {gloss('M 60,126 L 96,126 L 76,188 L 56,188 Q 50,144 60,126 Z', .4)}
    {face(240, 152, gap=38, eye_ry=15, mouth_dy=26, mouth_w=19, blush_gap=90, blush_dy=20)}
    '''


def train():
    puff = '<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"/>'
    return f'''
    {puff.format(cx=248, cy=54, r=20, fill=grad(*WHITE))}
    {puff.format(cx=206, cy=36, r=15, fill=grad(*WHITE))}
    {puff.format(cx=278, cy=30, r=13, fill=grad(*WHITE))}
    <circle cx="108" cy="236" r="34" fill="{grad(*DARK)}"/>
    <circle cx="108" cy="236" r="14" fill="{grad(*YELLOW)}" stroke-width="5"/>
    <circle cx="176" cy="236" r="34" fill="{grad(*DARK)}"/>
    <circle cx="176" cy="236" r="14" fill="{grad(*YELLOW)}" stroke-width="5"/>
    <circle cx="240" cy="244" r="24" fill="{grad(*DARK)}"/>
    <rect x="222" y="88" width="42" height="56" rx="9" fill="{grad(*DARK)}"/>
    <rect x="214" y="76" width="58" height="20" rx="9" fill="{grad(*DARK)}"/>
    <rect x="40" y="98" width="82" height="126" rx="16" fill="{grad('#ff6f61', '#c9382e')}"/>
    <rect x="56" y="118" width="50" height="42" rx="12" fill="{grad(*GLASS)}"/>
    <rect x="84" y="136" width="180" height="90" rx="28" fill="{grad(*RED)}"/>
    <rect x="84" y="212" width="184" height="20" rx="9" fill="{grad(*YELLOW)}"/>
    {gloss('M 106,148 Q 176,140 250,146 L 246,158 Q 176,152 108,160 Z', .4)}
    {face(178, 172, gap=40, eye_ry=15, mouth_dy=28, mouth_w=20, blush_gap=96, blush_dy=20)}
    '''


def bullet_train():
    return f'''
    <rect x="58" y="212" width="56" height="34" rx="14" fill="{grad(*DARK)}"/>
    <rect x="180" y="212" width="56" height="34" rx="14" fill="{grad(*DARK)}"/>
    <path d="M 32,104 L 194,104 Q 258,110 296,178 Q 304,194 286,194 L 32,194
             Q 20,194 20,182 L 20,116 Q 20,104 32,104 Z" fill="{grad(*WHITE)}"/>
    <path d="M 22,164 L 292,164 L 296,186 L 22,186 Z" fill="{grad(*BLUE)}" stroke="none"/>
    <path d="M 22,164 L 292,164 M 22,186 L 294,186" stroke-width="5"/>
    <g fill="{grad(*GLASS)}">
      <rect x="38" y="120" width="42" height="34" rx="12"/>
      <rect x="94" y="120" width="42" height="34" rx="12"/>
    </g>
    <path d="M 196,116 Q 240,124 268,158 L 196,158 Z" fill="{grad(*GLASS)}"/>
    {gloss('M 40,112 L 62,112 L 44,158 L 30,158 Q 28,124 40,112 Z', .45)}
    {face(228, 132, gap=36, eye_ry=14, mouth_dy=24, mouth_w=17, blush_gap=84, blush_dy=17)}
    '''


def helicopter():
    return f'''
    <rect x="118" y="72" width="20" height="34" rx="8" fill="{grad(*DARK)}"/>
    <rect x="16" y="60" width="252" height="16" rx="8" fill="{grad(*DARK)}"/>
    <ellipse cx="142" cy="68" rx="14" ry="12" fill="{grad(*GREY)}"/>
    <path d="M 176,152 L 288,138 Q 300,136 300,148 L 300,166 Q 300,178 288,180 L 176,196 Z"
          fill="{grad(*BLUE)}"/>
    <path d="M 276,142 L 296,92 L 312,138 Z" fill="{grad(*BLUE)}"/>
    <circle cx="288" cy="160" r="17" fill="{grad(*GREY)}" stroke-width="5"/>
    <path d="M 128,106 Q 202,106 208,168 Q 214,232 128,232 Q 56,232 56,168 Q 56,106 128,106 Z"
          fill="{grad(*RED)}"/>
    <path d="M 122,118 Q 186,120 192,166 Q 196,206 122,206 Q 68,206 68,166 Q 68,120 122,118 Z"
          fill="{grad(*GLASS)}"/>
    <path d="M 62,246 L 206,246" stroke-width="11" stroke-linecap="round"/>
    <path d="M 96,224 L 88,246 M 168,222 L 178,246" stroke-width="9" stroke-linecap="round"/>
    {gloss('M 88,130 L 116,128 L 78,196 L 68,178 Q 68,144 88,130 Z', .45)}
    {face(130, 156, gap=42, eye_ry=16, mouth_dy=30, mouth_w=22, blush_gap=104, blush_dy=22)}
    '''


def rocket():
    return f'''
    <path d="M 106,168 L 58,250 Q 54,258 62,256 L 108,240 Z" fill="{grad(*RED)}"/>
    <path d="M 214,168 L 262,250 Q 266,258 258,256 L 212,240 Z" fill="{grad(*RED)}"/>
    <path d="M 160,22 Q 214,84 214,178 L 214,236 Q 214,248 202,248 L 118,248
             Q 106,248 106,236 L 106,178 Q 106,84 160,22 Z" fill="{grad(*WHITE)}"/>
    <path d="M 160,22 Q 196,64 208,118 L 112,118 Q 124,64 160,22 Z" fill="{grad(*RED)}"/>
    <rect x="118" y="222" width="84" height="26" rx="11" fill="{grad(*GREY)}"/>
    <path d="M 132,256 Q 160,306 188,256 Z" fill="{grad(*YELLOW)}" stroke="none"/>
    <path d="M 144,256 Q 160,288 176,256 Z" fill="{grad(*ORANGE)}" stroke="none"/>
    {gloss('M 140,46 L 158,40 L 126,148 L 114,150 Q 116,90 140,46 Z', .4)}
    {face(160, 158, gap=40, eye_ry=16, mouth_dy=30, mouth_w=21, blush_gap=94, blush_dy=22)}
    '''


def satellite():
    panel = ('<rect x="{x}" y="120" width="94" height="76" rx="10" fill="{fill}"/>'
             '<path d="M {x},146 h94 M {x},170 h94 M {a},120 v76 M {b},120 v76" stroke-width="4"/>')
    return f'''
    <path d="M 160,96 Q 108,54 160,44 Q 212,54 160,96 Z" fill="{grad(*GREY)}"/>
    <path d="M 160,62 L 160,96" stroke-width="6"/>
    <circle cx="160" cy="52" r="9" fill="{grad(*DARK)}"/>
    <path d="M 108,158 L 122,158 M 198,158 L 212,158" stroke-width="9"/>
    {panel.format(x=16, a=47, b=78, fill=grad(*BLUE))}
    {panel.format(x=210, a=241, b=272, fill=grad(*BLUE))}
    <rect x="112" y="98" width="96" height="122" rx="24" fill="{grad(*YELLOW)}"/>
    <rect x="126" y="228" width="68" height="18" rx="8" fill="{grad(*DARK)}"/>
    {gloss('M 128,110 L 148,110 L 130,208 L 120,196 Q 118,132 128,110 Z', .4)}
    {face(160, 142, gap=40, eye_ry=15, mouth_dy=28, mouth_w=20, blush_gap=94, blush_dy=20)}
    '''


def submarine():
    return f'''
    <circle cx="36" cy="182" r="10" fill="{grad(*DARK)}"/>
    <path d="M 36,182 Q 6,146 18,140 Q 40,144 44,176 Z" fill="{grad(*GREY)}"/>
    <path d="M 36,182 Q 6,218 18,224 Q 40,220 44,188 Z" fill="{grad(*GREY)}"/>
    <rect x="162" y="52" width="16" height="44" rx="7" fill="{grad(*DARK)}"/>
    <rect x="162" y="52" width="46" height="16" rx="7" fill="{grad(*DARK)}"/>
    <path d="M 128,144 L 136,92 Q 138,80 152,80 L 194,80 Q 206,80 206,92 L 208,144 Z"
          fill="{grad('#ffd980', '#e8a318')}"/>
    <rect x="48" y="128" width="234" height="108" rx="54" fill="{grad(*YELLOW)}"/>
    <circle cx="94" cy="182" r="20" fill="{grad(*GLASS)}" stroke-width="6"/>
    <circle cx="272" cy="120" r="9" fill="{grad(*GLASS)}" opacity=".8"/>
    <circle cx="292" cy="92" r="6" fill="{grad(*GLASS)}" opacity=".8"/>
    {gloss('M 92,140 Q 170,132 246,140 L 242,152 Q 170,146 96,154 Z', .45)}
    {face(190, 174, gap=42, eye_ry=16, mouth_dy=30, mouth_w=22, blush_gap=104, blush_dy=22)}
    '''


def motorboat():
    return f'''
    <rect x="18" y="130" width="36" height="62" rx="15" fill="{grad(*DARK)}"/>
    <path d="M 34,192 L 50,236 L 20,236 Z" fill="{grad(*DARK)}"/>
    <path d="M 146,160 L 156,112 Q 158,102 170,102 L 214,102 Q 224,102 226,112 L 234,160 Z"
          fill="{grad(*GLASS)}"/>
    <path d="M 46,158 L 288,142 Q 302,140 296,158 L 270,226 Q 262,244 240,244 L 100,244
             Q 64,244 48,206 Z" fill="{grad(*WHITE)}"/>
    <path d="M 54,190 L 292,172 L 278,204 L 58,204 Z" fill="{grad(*RED)}" stroke="none"/>
    <path d="M 54,190 L 292,172 M 58,204 L 278,204" stroke-width="6"/>
    {gloss('M 74,166 Q 164,156 258,158 L 256,172 Q 164,168 84,180 Z', .45)}
    <path d="M 40,266 Q 94,252 148,266 M 178,268 Q 230,254 284,268"
          stroke-width="8" fill="none" opacity=".5" stroke-linecap="round"/>
    {face(168, 176, gap=42, eye_ry=15, mouth_dy=28, mouth_w=21, blush_gap=104, blush_dy=20)}
    '''


def canoe():
    return f'''
    <path d="M 236,64 L 288,196" stroke-width="12" stroke-linecap="round"/>
    <path d="M 274,178 Q 306,190 300,222 Q 274,232 264,206 Z" fill="{grad(*WOOD)}"/>
    <path d="M 22,142 Q 160,110 298,142 Q 292,166 258,206 Q 160,252 62,206 Q 28,166 22,142 Z"
          fill="{grad(*WOOD)}"/>
    <path d="M 44,152 Q 160,128 276,152 Q 268,168 244,192 Q 160,224 76,192 Q 52,168 44,152 Z"
          fill="{grad('#a75c2c', '#8a4620')}" stroke-width="5"/>
    <path d="M 96,166 L 106,190 M 224,166 L 214,190" stroke-width="9" stroke-linecap="round"/>
    {face(160, 176, gap=40, eye_ry=14, mouth_dy=26, mouth_w=19, blush_gap=96, blush_dy=18)}
    '''


def hangglider():
    return f'''
    <path d="M 160,58 L 20,184 Q 12,192 24,190 L 160,168 Z" fill="{grad(*RED)}"/>
    <path d="M 160,58 L 300,184 Q 308,192 296,190 L 160,168 Z" fill="{grad(*YELLOW)}"/>
    <path d="M 160,58 L 160,96" stroke-width="6"/>
    <path d="M 124,168 L 112,246 M 196,168 L 208,246" stroke-width="9" stroke-linecap="round"/>
    <path d="M 112,246 L 208,246" stroke-width="9" stroke-linecap="round"/>
    <path d="M 160,172 L 160,214" stroke-width="7"/>
    <circle cx="160" cy="222" r="12" fill="{grad(*MINT)}"/>
    {face(160, 124, gap=44, eye_ry=15, mouth_dy=28, mouth_w=21, blush_gap=108, blush_dy=20)}
    '''


VEHICLES = [
    ('자동차', car), ('자전거', bicycle), ('오토바이', motorcycle), ('버스', bus),
    ('선박', ship), ('비행기', airplane), ('기차', train), ('고속전철', bullet_train),
    ('헬리콥터', helicopter), ('우주선', rocket), ('위성', satellite), ('잠수함', submarine),
    ('모터보트', motorboat), ('카누', canoe), ('행글라이더', hangglider),
]


def build(name, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
            f'viewBox="0 0 {SIZE} {SIZE}"><defs>{"".join(_defs)}</defs>'
            f'<g stroke="{INK}" stroke-width="{SW}" stroke-linejoin="round" '
            f'stroke-linecap="round" fill="none">{body}</g></svg>')


if __name__ == '__main__':
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    only = sys.argv[2:]
    for name, builder in VEHICLES:
        if only and name not in only:
            continue
        _defs.clear()
        body = builder()
        svg = build(name, body)
        Path(out, name + '.svg').write_text(svg, encoding='utf-8')
        png = resvg_py.svg_to_bytes(svg_string=svg)
        Path(out, name + '.png').write_bytes(bytes(png))
        print('ok', name)
