#!/usr/bin/env python3
"""Generate a proper app icon for the ADB Tool.

Creates a source PNG at 1024x1024 with an Android robot design,
then generates all required macOS icon sizes and combines them into .icns.
Also creates a Windows .ico file.
"""

import os
import subprocess
import sys

# Try to import PIL/Pillow
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Installing Pillow...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image, ImageDraw, ImageFont


def create_android_icon(size=1024):
    """Create the Android-themed icon at the given size."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    s = size / 1024.0

    # Rounded square background (dark)
    bg_color = (20, 30, 25, 255)
    radius = int(220 * s)
    draw.rounded_rectangle(
        [(0, 0), (size, size)],
        radius=radius,
        fill=bg_color
    )

    # Green border
    border_color = (61, 220, 132, 255)
    border_width = int(8 * s)
    draw.rounded_rectangle(
        [(border_width//2, border_width//2), (size - border_width//2, size - border_width//2)],
        radius=radius - border_width//2,
        outline=border_color,
        width=border_width
    )

    # Android colors
    android_green = (61, 220, 132, 255)
    android_dark = (45, 155, 95, 255)

    # Antennae
    antenna_width = int(18 * s)
    left_ant_x1 = int(340 * s)
    left_ant_x2 = left_ant_x1 + int(20 * s)
    left_ant_y1 = int(210 * s)
    left_ant_y2 = int(300 * s)
    draw.rounded_rectangle(
        [(left_ant_x1, left_ant_y1), (left_ant_x2, left_ant_y2)],
        radius=antenna_width // 2,
        fill=android_green
    )
    right_ant_x1 = int(664 * s)
    right_ant_x2 = right_ant_x1 + int(20 * s)
    right_ant_y1 = int(210 * s)
    right_ant_y2 = int(300 * s)
    draw.rounded_rectangle(
        [(right_ant_x1, right_ant_y1), (right_ant_x2, right_ant_y2)],
        radius=antenna_width // 2,
        fill=android_green
    )

    # Head
    head_top = int(280 * s)
    head_bottom = int(640 * s)
    head_left = int(220 * s)
    head_right = int(804 * s)
    head_radius = int(140 * s)
    draw.rounded_rectangle(
        [(head_left, head_top), (head_right, head_bottom)],
        radius=head_radius,
        fill=android_green
    )

    # Eyes
    eye_radius = int(45 * s)
    eye_y = int(400 * s)
    left_eye_x = int(390 * s)
    draw.ellipse(
        [(left_eye_x - eye_radius, eye_y - eye_radius),
         (left_eye_x + eye_radius, eye_y + eye_radius)],
        fill=(255, 255, 255, 255)
    )
    right_eye_x = int(634 * s)
    draw.ellipse(
        [(right_eye_x - eye_radius, eye_y - eye_radius),
         (right_eye_x + eye_radius, eye_y + eye_radius)],
        fill=(255, 255, 255, 255)
    )

    # Cable/plug connector at bottom
    plug_y_top = int(680 * s)
    plug_y_bottom = int(820 * s)
    plug_width = int(220 * s)
    plug_height = plug_y_bottom - plug_y_top
    plug_left = (size - plug_width) // 2
    plug_right = plug_left + plug_width
    draw.rounded_rectangle(
        [(plug_left, plug_y_top), (plug_right, plug_y_bottom)],
        radius=int(30 * s),
        fill=android_dark
    )

    # Terminal prompt ">_" on plug
    try:
        font_paths = [
            '/System/Library/Fonts/Menlo.ttc',
            '/System/Library/Fonts/Helvetica.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
            'C:\\Windows\\Fonts\\consola.ttf',
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, int(90 * s))
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    prompt = ">_"
    try:
        bbox = draw.textbbox((0, 0), prompt, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = int(140 * s), int(90 * s)
    text_x = (size - text_w) // 2 - int(10 * s)
    text_y = plug_y_top + (plug_height - text_h) // 2 - int(15 * s)
    draw.text((text_x, text_y), prompt, fill=(255, 255, 255, 255), font=font)

    return img


def generate_mac_iconset(source_img, iconset_dir):
    """Generate all required macOS icon sizes from a source image."""
    sizes = [
        (16, '16x16'),
        (32, '16x16@2x'),
        (32, '32x32'),
        (64, '32x32@2x'),
        (64, '64x64'),
        (128, '64x64@2x'),
        (128, '128x128'),
        (256, '128x128@2x'),
        (256, '256x256'),
        (512, '256x256@2x'),
        (512, '512x512'),
        (1024, '512x512@2x'),
        (1024, '1024x1024'),
    ]

    os.makedirs(iconset_dir, exist_ok=True)

    written = set()
    for size, name in sizes:
        if size in written:
            continue
        resized = source_img.resize((size, size), Image.LANCZOS)
        out_path = os.path.join(iconset_dir, f'icon_{name}.png')
        resized.save(out_path, 'PNG')
        written.add(size)
        print(f"  Created {out_path} ({size}x{size})")


def create_icns_from_iconset(iconset_dir, output_icns):
    """Convert an iconset directory to a .icns file using macOS iconutil."""
    print(f"\nCreating .icns from iconset...")
    try:
        subprocess.check_call(['iconutil', '-c', 'icns', iconset_dir, '-o', output_icns])
        print(f"  Created {output_icns}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  iconutil not available (not on macOS)")
        return False


def create_ico(source_img, output_ico, sizes=None):
    """Create a Windows .ico file from the source image."""
    if sizes is None:
        sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    print(f"\nCreating Windows .ico file...")
    images = []
    for size in sizes:
        img = source_img.resize(size, Image.LANCZOS)
        images.append(img)
    images[0].save(output_ico, format='ICO', sizes=[i.size for i in images])
    print(f"  Created {output_ico}")


def main():
    project_dir = '/Users/sainis/Documents/GitHub/GUI-for-Android-Debug-Bridge-ADB-UI'
    icon_dir = os.path.join(project_dir, 'icons')
    os.makedirs(icon_dir, exist_ok=True)

    print("=" * 60)
    print("ADB Tool - Icon Generator")
    print("=" * 60)

    print("\n[1/4] Generating source icon (1024x1024)...")
    source = create_android_icon(1024)
    source_png = os.path.join(icon_dir, 'icon.png')
    source.save(source_png, 'PNG')
    print(f"  Created {source_png}")

    print("\n[2/4] Generating macOS iconset...")
    iconset_dir = os.path.join(icon_dir, 'icon.iconset')
    generate_mac_iconset(source, iconset_dir)

    print("\n[3/4] Creating .icns file...")
    icns_path = os.path.join(icon_dir, 'icon.icns')
    icns_created = create_icns_from_iconset(iconset_dir, icns_path)
    if not icns_created:
        icns_path = source_png

    print("\n[4/4] Creating Windows .ico file...")
    ico_path = os.path.join(icon_dir, 'icon.ico')
    create_ico(source, ico_path)

    print("\n[Bonus] Updating ADB Tool.spec...")
    spec_path = os.path.join(project_dir, 'ADB Tool.spec')
    update_spec_file(spec_path, icns_path)

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
    print(f"\nGenerated files in: {icon_dir}/")
    print(f"  - icon.png        (1024x1024 source)")
    print(f"  - icon.icns       (macOS app icon)")
    print(f"  - icon.ico        (Windows app icon)")
    print(f"  - icon.iconset/   (individual PNG sizes)")
    print(f"\nNext steps:")
    print(f"  1. Rebuild the app: pyinstaller 'ADB Tool.spec' --clean")
    print(f"  2. The new icon will be embedded in the .app bundle")


def update_spec_file(spec_path, icon_path):
    """Update the PyInstaller spec file to use the new icon."""
    with open(spec_path, 'r') as f:
        content = f.read()

    new_content = content.replace("icon=None", f"icon='{icon_path}'")

    if new_content != content:
        with open(spec_path, 'w') as f:
            f.write(new_content)
        print(f"  Updated {spec_path}")
    else:
        print(f"  Spec file already configured or no change needed")


if __name__ == '__main__':
    main()
