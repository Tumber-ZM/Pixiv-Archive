"""生成 assets/icon.ico（蓝色圆角方块 + 字母 P，贴合 Pixiv 蓝主题）。"""
import os
from PIL import Image, ImageDraw, ImageFont

ACCENT = (59, 158, 255, 255)
BG = (16, 18, 24, 255)


def make_icon(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 8
    d.rounded_rectangle((r, r, size - r, size - r), radius=size // 6, fill=BG)
    m = size // 6
    d.rounded_rectangle(
        (m, m, size - m, size - m), radius=size // 7,
        outline=ACCENT, width=max(2, size // 40),
    )
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", int(size * 0.62))
        except Exception:
            font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "P", font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1] - size * 0.02
    d.text((x, y), "P", font=font, fill=ACCENT)
    return img


def main():
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(project, "assets")
    os.makedirs(out_dir, exist_ok=True)
    img = make_icon(256)
    png_path = os.path.join(out_dir, "icon.png")
    ico_path = os.path.join(out_dir, "icon.ico")
    img.save(png_path)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico_path, format="ICO", sizes=sizes)
    print("wrote", png_path)
    print("wrote", ico_path)


if __name__ == "__main__":
    main()
