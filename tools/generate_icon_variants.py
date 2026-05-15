from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
import PIL.IcnsImagePlugin  # noqa: F401 - registers ICNS writer


BASE_SIZE = 1024
SCALE = 4
DRAW_SIZE = BASE_SIZE * SCALE
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512, 1024]


Color = tuple[int, int, int, int]
Box = tuple[float, float, float, float]


def rgba(hex_color: str, alpha: int = 255) -> Color:
    hex_color = hex_color.lstrip("#")
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
        alpha,
    )


def s(value: float) -> int:
    return round(value * SCALE)


def sb(box: Box) -> tuple[int, int, int, int]:
    return tuple(s(v) for v in box)


def rounded_mask(radius: float, box: Box | None = None) -> Image.Image:
    mask = Image.new("L", (DRAW_SIZE, DRAW_SIZE), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(sb(box or (0, 0, BASE_SIZE, BASE_SIZE)), radius=s(radius), fill=255)
    return mask


def circle_mask(box: Box) -> Image.Image:
    mask = Image.new("L", (DRAW_SIZE, DRAW_SIZE), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(sb(box), fill=255)
    return mask


def diagonal_gradient(top_left: str, bottom_right: str) -> Image.Image:
    c0 = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), rgba(top_left))
    c1 = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), rgba(bottom_right))
    vertical = Image.linear_gradient("L").resize((DRAW_SIZE, DRAW_SIZE))
    horizontal = Image.linear_gradient("L").rotate(90).resize((DRAW_SIZE, DRAW_SIZE))
    mask = ImageChops.add(vertical.point(lambda p: p // 2), horizontal.point(lambda p: p // 2))
    return Image.composite(c1, c0, mask)


def composite_shadow(
    base: Image.Image,
    mask: Image.Image,
    offset: tuple[int, int] = (0, 28),
    blur: int = 34,
    opacity: int = 95,
) -> None:
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shifted = Image.new("L", base.size, 0)
    shifted.paste(mask, (s(offset[0]), s(offset[1])))
    shifted = shifted.filter(ImageFilter.GaussianBlur(s(blur)))
    shadow.putalpha(shifted.point(lambda p: min(opacity, p * opacity // 255)))
    base.alpha_composite(shadow)


def add_background(
    base: Image.Image,
    shape: str,
    gradient: tuple[str, str],
    inset: float = 56,
    radius: float = 210,
) -> None:
    box = (inset, inset, BASE_SIZE - inset, BASE_SIZE - inset)
    mask = circle_mask(box) if shape == "circle" else rounded_mask(radius, box)
    composite_shadow(base, mask, offset=(0, 24), blur=40, opacity=80)
    fill = diagonal_gradient(*gradient)
    shaped = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shaped.alpha_composite(fill)
    shaped.putalpha(mask)
    base.alpha_composite(shaped)

    shine = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shine_draw = ImageDraw.Draw(shine)
    shine_draw.ellipse(sb((104, 90, 682, 510)), fill=(255, 255, 255, 32))
    shine.putalpha(ImageChops.multiply(shine.getchannel("A"), mask))
    base.alpha_composite(shine)


def draw_round_rect(
    draw: ImageDraw.ImageDraw,
    box: Box,
    radius: float,
    fill: Color | None,
    outline: Color | None = None,
    width: float = 1,
) -> None:
    draw.rounded_rectangle(sb(box), radius=s(radius), fill=fill, outline=outline, width=s(width))


def draw_bottle(
    layer: Image.Image,
    x: float,
    y: float,
    width: float,
    height: float,
    liquid: str,
    outline: Color = (255, 255, 255, 232),
    glass: Color = (255, 255, 255, 46),
) -> None:
    draw = ImageDraw.Draw(layer)
    body_x0 = x + width * 0.18
    body_x1 = x + width * 0.82
    body_y0 = y + height * 0.28
    body_y1 = y + height * 0.96
    neck_x0 = x + width * 0.35
    neck_x1 = x + width * 0.65
    neck_y0 = y + height * 0.12
    neck_y1 = body_y0 + height * 0.02
    cap_x0 = x + width * 0.27
    cap_x1 = x + width * 0.73
    cap_y0 = y + height * 0.04
    cap_y1 = y + height * 0.14

    draw_round_rect(draw, (body_x0, body_y0, body_x1, body_y1), 36, glass, outline, 10)
    draw_round_rect(draw, (neck_x0, neck_y0, neck_x1, neck_y1), 12, (255, 255, 255, 38), outline, 8)
    draw_round_rect(draw, (cap_x0, cap_y0, cap_x1, cap_y1), 14, (255, 255, 255, 180), None)

    liquid_top = body_y0 + (body_y1 - body_y0) * 0.48
    liquid_box = (body_x0 + 18, liquid_top, body_x1 - 18, body_y1 - 18)
    draw_round_rect(draw, liquid_box, 24, rgba(liquid, 150), None)
    draw.arc(sb((liquid_box[0], liquid_top - 18, liquid_box[2], liquid_top + 46)), 0, 180, fill=rgba(liquid, 235), width=s(8))

    draw.line(sb((body_x0 + 32, body_y0 + 48, body_x0 + 32, body_y1 - 70)), fill=(255, 255, 255, 118), width=s(9))
    draw.ellipse(sb((x + width * 0.43, y + height * 0.58, x + width * 0.49, y + height * 0.64)), fill=(255, 255, 255, 170))


def draw_label(
    layer: Image.Image,
    x: float,
    y: float,
    width: float,
    height: float,
    accent: str,
    angle: float = -8,
) -> None:
    tag = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(tag)
    draw_round_rect(draw, (x, y, x + width, y + height), 28, (255, 255, 255, 220), None)
    draw.ellipse(sb((x + width * 0.42, y + height * 0.09, x + width * 0.58, y + height * 0.25)), fill=rgba(accent, 245))
    for idx, line_width in enumerate((0.68, 0.72, 0.48)):
        yy = y + height * (0.40 + idx * 0.18)
        xx = x + width * 0.16
        draw.line(sb((xx, yy, xx + width * line_width, yy)), fill=rgba(accent, 190), width=s(13))
    if angle:
        tag = tag.rotate(angle, resample=Image.Resampling.BICUBIC, center=(s(x + width / 2), s(y + height / 2)))
    layer.alpha_composite(tag)


def draw_sheet(layer: Image.Image, x: float, y: float, width: float, height: float, accent: str) -> None:
    draw = ImageDraw.Draw(layer)
    draw_round_rect(draw, (x, y, x + width, y + height), 34, (255, 255, 255, 226), None)
    draw_round_rect(draw, (x + 32, y + 38, x + width - 32, y + 112), 14, rgba(accent, 215), None)
    for col in range(4):
        xx = x + 54 + col * ((width - 108) / 4)
        draw.line(sb((xx, y + 150, xx, y + height - 42)), fill=(41, 70, 86, 56), width=s(5))
    for row in range(5):
        yy = y + 160 + row * ((height - 214) / 5)
        draw.line(sb((x + 42, yy, x + width - 42, yy)), fill=(41, 70, 86, 52), width=s(5))


def draw_photo_stack(layer: Image.Image, x: float, y: float, width: float, height: float, accent: str) -> None:
    draw = ImageDraw.Draw(layer)
    draw_round_rect(draw, (x + 48, y - 12, x + width + 48, y + height - 12), 36, (18, 54, 72, 80), None)
    draw_round_rect(draw, (x + 26, y + 20, x + width + 26, y + height + 20), 36, (255, 255, 255, 122), None)
    draw_round_rect(draw, (x, y, x + width, y + height), 38, (255, 255, 255, 230), None)
    draw_round_rect(draw, (x + 32, y + 34, x + width - 32, y + height - 76), 26, rgba(accent, 222), None)
    draw.ellipse(sb((x + 68, y + 72, x + 130, y + 134)), fill=(255, 255, 255, 190))
    draw.polygon(
        [tuple(map(s, p)) for p in ((x + 62, y + height - 96), (x + width * 0.44, y + height * 0.45), (x + width - 62, y + height - 96))],
        fill=(255, 255, 255, 180),
    )


def draw_magnifier(layer: Image.Image, x: float, y: float, radius: float) -> None:
    draw = ImageDraw.Draw(layer)
    draw.ellipse(sb((x - radius, y - radius, x + radius, y + radius)), outline=(255, 255, 255, 238), width=s(18))
    draw.line(sb((x + radius * 0.70, y + radius * 0.70, x + radius * 1.45, y + radius * 1.45)), fill=(255, 255, 255, 238), width=s(24))
    draw.line(sb((x - radius * 0.30, y, x + radius * 0.35, y)), fill=(255, 255, 255, 170), width=s(10))


def draw_database(layer: Image.Image, x: float, y: float, width: float, height: float, accent: str) -> None:
    draw = ImageDraw.Draw(layer)
    fill = (255, 255, 255, 228)
    side = (255, 255, 255, 150)
    outline = rgba(accent, 210)
    ellipse_h = height * 0.24
    draw.rectangle(sb((x, y + ellipse_h / 2, x + width, y + height - ellipse_h / 2)), fill=fill)
    draw.ellipse(sb((x, y, x + width, y + ellipse_h)), fill=fill, outline=outline, width=s(8))
    for frac in (0.38, 0.66):
        cy = y + height * frac
        draw.arc(sb((x, cy - ellipse_h / 2, x + width, cy + ellipse_h / 2)), 0, 180, fill=outline, width=s(8))
    draw.ellipse(sb((x, y + height - ellipse_h, x + width, y + height)), fill=side, outline=outline, width=s(8))


def draw_check(layer: Image.Image, x: float, y: float, scale: float = 1.0) -> None:
    draw = ImageDraw.Draw(layer)
    points = [(x, y + 42 * scale), (x + 48 * scale, y + 88 * scale), (x + 144 * scale, y)]
    draw.line([tuple(map(s, p)) for p in points], fill=(255, 255, 255, 242), width=s(24 * scale), joint="curve")


def finish(img: Image.Image) -> Image.Image:
    return img.resize((BASE_SIZE, BASE_SIZE), Image.Resampling.LANCZOS)


def icon_specimen_blue() -> Image.Image:
    img = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), (0, 0, 0, 0))
    add_background(img, "rounded", ("#155d88", "#29b5a8"), inset=54, radius=216)
    draw_bottle(img, 258, 190, 318, 628, liquid="#7dd3fc")
    draw_label(img, 552, 354, 252, 294, accent="#155d88", angle=-7)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw_round_rect(draw, (264, 696, 754, 798), 34, (13, 43, 64, 110), None)
    draw.line(sb((310, 742, 704, 742)), fill=(255, 255, 255, 120), width=s(12))
    img.alpha_composite(layer)
    return finish(img)


def icon_ledger_green() -> Image.Image:
    img = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), (0, 0, 0, 0))
    add_background(img, "rounded", ("#0f766e", "#365314"), inset=58, radius=190)
    sheet = Image.new("RGBA", img.size, (0, 0, 0, 0))
    composite_shadow(img, rounded_mask(34, (190, 178, 716, 744)), offset=(0, 18), blur=24, opacity=72)
    draw_sheet(sheet, 190, 178, 526, 566, accent="#16a34a")
    img.alpha_composite(sheet)
    draw_bottle(img, 472, 270, 282, 548, liquid="#a3e635")
    draw_label(img, 260, 604, 264, 180, accent="#166534", angle=6)
    draw_check(img, 682, 680, scale=0.86)
    return finish(img)


def icon_photo_coral() -> Image.Image:
    img = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), (0, 0, 0, 0))
    add_background(img, "circle", ("#ef7d50", "#0f8b8d"), inset=62)
    stack = Image.new("RGBA", img.size, (0, 0, 0, 0))
    composite_shadow(img, rounded_mask(38, (200, 212, 730, 708)), offset=(0, 26), blur=28, opacity=72)
    draw_photo_stack(stack, 206, 226, 506, 430, accent="#0f8b8d")
    img.alpha_composite(stack)
    draw_bottle(img, 384, 340, 220, 420, liquid="#fb923c")
    draw_magnifier(img, 680, 648, 104)
    return finish(img)


def icon_archive_indigo() -> Image.Image:
    img = Image.new("RGBA", (DRAW_SIZE, DRAW_SIZE), (0, 0, 0, 0))
    add_background(img, "rounded", ("#4338ca", "#0891b2"), inset=54, radius=210)
    composite_shadow(img, rounded_mask(70, (206, 242, 666, 760)), offset=(0, 22), blur=28, opacity=60)
    draw_database(img, 208, 250, 456, 500, accent="#3730a3")
    draw_bottle(img, 514, 186, 278, 600, liquid="#67e8f9")
    draw_label(img, 314, 604, 276, 178, accent="#3730a3", angle=-5)
    return finish(img)


VARIANTS = {
    "specimen_blue": icon_specimen_blue,
    "ledger_green": icon_ledger_green,
    "photo_coral": icon_photo_coral,
    "archive_indigo": icon_archive_indigo,
}


def save_variant(name: str, image: Image.Image, output_dir: Path) -> list[Path]:
    variant_dir = output_dir / name
    linux_dir = variant_dir / "linux_hicolor"
    variant_dir.mkdir(parents=True, exist_ok=True)
    linux_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    png_path = variant_dir / f"{name}_1024.png"
    image.save(png_path)
    paths.append(png_path)

    ico_path = variant_dir / f"{name}.ico"
    image.save(ico_path, format="ICO", sizes=ICO_SIZES)
    paths.append(ico_path)

    icns_path = variant_dir / f"{name}.icns"
    image.save(icns_path, format="ICNS")
    paths.append(icns_path)

    for size in PNG_SIZES:
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        size_dir = linux_dir / f"{size}x{size}" / "apps"
        size_dir.mkdir(parents=True, exist_ok=True)
        linux_path = size_dir / "specimen-organise.png"
        resized.save(linux_path)
        paths.append(linux_path)

    return paths


def save_preview(images: dict[str, Image.Image], output_dir: Path) -> Path:
    tile = 300
    gap = 54
    label_h = 56
    width = gap + len(images) * tile + (len(images) - 1) * gap + gap
    height = gap + tile + label_h + gap
    preview = Image.new("RGBA", (width, height), (248, 250, 252, 255))
    draw = ImageDraw.Draw(preview)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    x = gap
    for name, image in images.items():
        preview.alpha_composite(image.resize((tile, tile), Image.Resampling.LANCZOS), (x, gap))
        label = name.replace("_", " ")
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text((x + (tile - (bbox[2] - bbox[0])) / 2, gap + tile + 18), label, fill=(15, 23, 42, 255), font=font)
        x += tile + gap
    path = output_dir / "icon_variants_preview.png"
    preview.convert("RGB").save(path)
    return path


def write_index(output_dir: Path, generated: dict[str, list[Path]], preview: Path) -> Path:
    lines = [
        "# App Icon Variants",
        "",
        "Generated icon candidates for the specimen intake desktop app.",
        "",
        "Each variant contains:",
        "- `<variant>.ico` for Windows installers and PyInstaller.",
        "- `<variant>.icns` for macOS app bundles.",
        "- `linux_hicolor/<size>x<size>/apps/specimen-organise.png` for Linux desktop packaging.",
        "- `<variant>_1024.png` as the source-size PNG.",
        "",
        f"Preview sheet: `{preview.name}`",
        "",
        "Regenerate:",
        "",
        "```bash",
        "python tools/generate_icon_variants.py",
        "```",
        "",
        "Use a variant for PyInstaller:",
        "",
        "```bash",
        "python build_release.py --icon assets/icons/app-icon-variants/specimen_blue/specimen_blue.ico",
        "python build_release.py --icon assets/icons/app-icon-variants/specimen_blue/specimen_blue.icns",
        "```",
        "",
        "For Linux packaging, install the chosen `linux_hicolor/` tree into the package icon theme path",
        "and set the desktop file icon name to `specimen-organise`.",
        "",
        "Variants:",
    ]
    for name in generated:
        lines.append(f"- `{name}`")
    lines.append("")
    path = output_dir / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate app icon variants for packaging.")
    parser.add_argument(
        "--output-dir",
        default="assets/icons/app-icon-variants",
        help="Directory for generated icon candidates.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    images = {name: factory() for name, factory in VARIANTS.items()}
    generated = {name: save_variant(name, image, output_dir) for name, image in images.items()}
    preview = save_preview(images, output_dir)
    index = write_index(output_dir, generated, preview)

    print(f"Generated {len(generated)} icon variants in {output_dir}")
    print(f"Preview: {preview}")
    print(f"Index: {index}")


if __name__ == "__main__":
    main()
