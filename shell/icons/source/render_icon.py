"""Render the app icon master from the mascot grid (TD-5196).

The padlock with two eyes is the That Simple Tech logo. This draws the same
24-pixel grid as ``ui/src/lib/mascot.ts`` (eyes open, looking ahead) in
white on a black rounded square laid out on Apple's 1024-pixel icon grid,
matching the site's touch icon. ``npx tauri icon`` turns the output into
every size in ``shell/icons``.

Usage: uv run --project core python shell/icons/source/render_icon.py OUT.png
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw

SHACKLE = [
    "........########........",
    "......############......",
    ".....##############.....",
    ".....####......####.....",
    "....####........####....",
    "....###..........###....",
    "....###..........###....",
    "....###..........###....",
    "....###..........###....",
    "........................",
]
BODY = [
    "..####################..",
    ".######################.",
    *["########################"] * 11,
    ".######################.",
    "..####################..",
]
EYE = [".##.", "####", "####", "####", "####", ".##."]
EYE_TOP, EYE_COLUMNS = 14, (5, 15)

CANVAS = 1024
# Apple's macOS icon grid: an 824-pixel rounded square, 100 pixels in.
INSET, RADIUS = 100, 185
CELL = 22  # whole pixels per art pixel keeps every edge sharp


def grid() -> list[list[bool]]:
    rows = [[c == "#" for c in row] for row in SHACKLE + BODY]
    for left in EYE_COLUMNS:
        for dy, line in enumerate(EYE):
            for dx, c in enumerate(line):
                if c == "#":
                    rows[EYE_TOP + dy][left + dx] = False
    return rows


def render(path: str) -> None:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (INSET, INSET, CANVAS - INSET - 1, CANVAS - INSET - 1),
        RADIUS,
        fill=(0, 0, 0, 255),
    )
    cells = grid()
    left = (CANVAS - len(cells[0]) * CELL) // 2
    top = (CANVAS - len(cells) * CELL) // 2
    for y, row in enumerate(cells):
        for x, on in enumerate(row):
            if on:
                x0, y0 = left + x * CELL, top + y * CELL
                draw.rectangle(
                    (x0, y0, x0 + CELL - 1, y0 + CELL - 1), fill=(255, 255, 255, 255)
                )
    image.save(path)


if __name__ == "__main__":
    render(sys.argv[1])
