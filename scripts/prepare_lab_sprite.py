#!/usr/bin/env python3
"""Turn a flattened, labelled sprite sheet image into a uniform transparent PNG grid for the Crawler lab.

Handles sheets exported as JPEG with a painted checkerboard "transparency", uneven cell widths
and row labels drawn over the cells. Output: rows x columns frames of --size px, each frame
aligned on the bottom-centre of its sprite (the hover shadow), so animations don't jitter.

    python3 scripts/prepare_lab_sprite.py static/local_assets/robots/probe_asset_1.jpeg

Needs Pillow and numpy (a developer tool; the app itself does not import it). Input and output stay
under the git-ignored static/local_assets/ folder.
"""
import argparse
from pathlib import Path

import numpy as np
from PIL import Image


def runs(mask):
    """[(start, end)] for consecutive True runs in a 1-D boolean array."""
    edges = np.flatnonzero(np.diff(np.r_[0, mask.astype(int), 0]))
    return list(zip(edges[::2], edges[1::2]))


def grid_lines(dark, axis, threshold, max_width=10):
    """Centres of thin dark lines spanning most of the image along ``axis``. The width limit keeps a
    dark sprite body that happens to fill a column from being mistaken for a grid line."""
    coverage = dark.mean(axis=axis)
    return [int((a + b) / 2) for a, b in runs(coverage > threshold) if b - a <= max_width]


def foreground(rgb):
    """Everything that isn't the grey checkerboard: low-saturation mid/light greys are background."""
    value, low = rgb.max(axis=2).astype(int), rgb.min(axis=2).astype(int)
    checker = ((value - low) < 22) & (value >= 110) & (value <= 228)
    return ~checker


def components(mask):
    """4-connected components as (ys, xs) index arrays; small pure-numpy flood fill."""
    labels = np.zeros(mask.shape, dtype=np.int32)
    found, current = [], 0
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x]:
            continue
        current += 1
        stack, ys, xs = [(y, x)], [], []
        labels[y, x] = current
        while stack:
            cy, cx = stack.pop()
            ys.append(cy)
            xs.append(cx)
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = current
                    stack.append((ny, nx))
        found.append((np.array(ys), np.array(xs)))
    return found


def extract_cell(rgb, top, bottom, left, right, inset=6):
    cell = rgb[top + inset:bottom - inset, left + inset:right - inset]
    mask = foreground(cell)
    height = cell.shape[0]
    keep = np.zeros_like(mask)
    for ys, xs in components(mask):
        if len(ys) < 25:
            continue  # JPEG speckle
        if ys.max() < height * 0.3:
            continue  # row label text drawn across the top of the cell
        keep[ys, xs] = True
    if not keep.any():
        return None
    alpha = (keep * 255).astype(np.uint8)
    return np.dstack([cell, alpha])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, help="Output PNG (default: next to the source, .png)")
    parser.add_argument("--size", type=int, default=48, help="Output frame size in px (square)")
    parser.add_argument("--columns", type=int, default=5, help="Frames to keep per row")
    args = parser.parse_args()

    rgb = np.asarray(Image.open(args.source).convert("RGB"))
    dark = rgb.max(axis=2) < 70
    rows = grid_lines(dark, axis=1, threshold=0.6)
    # Rows are the bands between consecutive horizontal lines that are tall enough to hold frames.
    bands = [(a, b) for a, b in zip(rows, rows[1:]) if b - a > rgb.shape[0] * 0.08]
    # The column grid is shared by all rows. Labels can hide part of a real line in one row and a dark
    # sprite body can look like a line in another, so keep only lines found in most rows.
    per_band = [grid_lines(dark[top + 4:bottom - 4], axis=0, threshold=0.6) for top, bottom in bands]
    candidates = sorted({x for lines in per_band for x in lines})
    columns = []
    for x in candidates:
        if columns and x - columns[-1] <= 6:
            continue
        if sum(any(abs(x - other) <= 6 for other in lines) for lines in per_band) * 2 > len(bands):
            columns.append(x)
    frames = []
    for top, bottom in bands:
        cells = [(a, b) for a, b in zip(columns, columns[1:]) if b - a > 40]
        row = [extract_cell(rgb, top, bottom, a, b) for a, b in cells]
        row = [frame for frame in row if frame is not None][:args.columns]
        if row:
            frames.append(row)
    if not frames:
        raise SystemExit("No frames found; check the sheet's grid lines.")

    # One scale for every frame (from the largest sprite) so the bot keeps its size between animations.
    crops = []
    for row in frames:
        cropped = []
        for frame in row:
            ys, xs = np.nonzero(frame[:, :, 3])
            cropped.append(frame[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
        crops.append(cropped)
    largest = max(max(c.shape[0], c.shape[1]) for row in crops for c in row)
    scale = (args.size - 2) / largest
    sheet = Image.new("RGBA", (args.size * args.columns, args.size * len(crops)), (0, 0, 0, 0))
    for r, row in enumerate(crops):
        for c, crop in enumerate(row):
            image = Image.fromarray(crop, "RGBA")
            width, height = max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale))
            image = image.resize((width, height), Image.BOX)
            pixels = np.asarray(image).copy()
            pixels[:, :, 3] = np.where(pixels[:, :, 3] >= 128, 255, 0)  # crisp pixel-art edges
            image = Image.fromarray(pixels, "RGBA")
            # Bottom-centre anchor: the shadow sits on the frame's floor line.
            sheet.paste(image, (c * args.size + (args.size - width) // 2, r * args.size + args.size - 1 - height), image)
    out = args.out or args.source.with_suffix(".png")
    sheet.save(out)
    print(f"{out}: {len(crops)} row(s) x {args.columns} column(s) of {args.size}px "
          f"(frames per row: {[len(row) for row in crops]})")


if __name__ == "__main__":
    main()
