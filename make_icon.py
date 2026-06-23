"""Generates lingua.ico using PyQt6 — no extra dependencies needed."""
import struct
import sys
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QBrush
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)

BG   = QColor("#0c0c11")
GOLD = QColor("#c4a35a")


def draw_icon(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)  # transparent

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)

    # Rounded square background
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, s, s), s * 0.18, s * 0.18)
    p.fillPath(bg, BG)

    # Speech-bubble body
    pad = s * 0.12
    bw  = s - 2 * pad
    bh  = s * 0.56
    bx, by = pad, pad * 0.85
    br  = s * 0.10

    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(bx, by, bw, bh), br, br)
    p.fillPath(bubble, GOLD)

    # Triangle tail (bottom-left)
    tail = QPainterPath()
    tail.moveTo(bx + bw * 0.15, by + bh)
    tail.lineTo(bx + bw * 0.38, by + bh)
    tail.lineTo(bx + bw * 0.08, by + bh + s * 0.16)
    tail.closeSubpath()
    p.fillPath(tail, GOLD)

    # Text lines inside bubble
    if size >= 24:
        lpad  = bw * 0.18
        lh    = max(2.0, s * 0.055)
        gap   = bh * 0.27
        for i in range(3):
            x1 = bx + lpad
            x2 = bx + bw - lpad - i * bw * 0.16
            ly = by + bh * 0.19 + i * gap
            if x2 > x1 and ly + lh < by + bh - lh:
                rct = QRectF(x1, ly, x2 - x1, lh)
                ln  = QPainterPath()
                ln.addRoundedRect(rct, lh / 2, lh / 2)
                p.fillPath(ln, QBrush(BG))

    p.end()
    return img


def to_png_bytes(img: QImage) -> bytes:
    ba  = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


SIZES = [256, 64, 48, 32, 16]
blobs = [to_png_bytes(draw_icon(sz)) for sz in SIZES]

# ICO format: header + directory entries + PNG blobs
header  = struct.pack("<HHH", 0, 1, len(SIZES))
dir_off = 6 + len(SIZES) * 16
entries = b""
data    = b""

for sz, blob in zip(SIZES, blobs):
    w = 0 if sz == 256 else sz
    h = 0 if sz == 256 else sz
    entries += struct.pack("<BBBBHHII",
        w, h, 0, 0, 1, 32,
        len(blob),
        dir_off + len(data),
    )
    data += blob

with open("lingua.ico", "wb") as f:
    f.write(header + entries + data)

print("lingua.ico created successfully.")
app.quit()
