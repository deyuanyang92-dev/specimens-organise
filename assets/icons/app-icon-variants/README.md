# App Icon Variants

Generated icon candidates for the specimen intake desktop app.

Each variant contains:
- `<variant>.ico` for Windows installers and PyInstaller.
- `<variant>.icns` for macOS app bundles.
- `linux_hicolor/<size>x<size>/apps/specimen-organise.png` for Linux desktop packaging.
- `<variant>_1024.png` as the source-size PNG.

Preview sheet: `icon_variants_preview.png`

Regenerate:

```bash
python tools/generate_icon_variants.py
```

Use a variant for PyInstaller:

```bash
python build_release.py --icon assets/icons/app-icon-variants/specimen_blue/specimen_blue.ico
python build_release.py --icon assets/icons/app-icon-variants/specimen_blue/specimen_blue.icns
```

For Linux packaging, install the chosen `linux_hicolor/` tree into the package icon theme path
and set the desktop file icon name to `specimen-organise`.

Variants:
- `specimen_blue`
- `ledger_green`
- `photo_coral`
- `archive_indigo`
