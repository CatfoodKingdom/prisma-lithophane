# Prisma

Prisma is a local application for making full-color lithophanes with an FDM
printer. It runs on your computer and serves its interface only on the local
loopback address; it is not a hosted web service, and uploaded images stay on
your machine.

Prisma has two applications:

- **Prisma Generator** converts an image into printable 3MF/STL files, color
  layers, and filament-swap instructions using a published color-model bundle.
- **Prisma Calibration** measures printed filament samples, fits private
  working models, and publishes complete immutable model bundles for Generator.

The applications do not share live working data. Generator only consumes a
published bundle selected through its Model Library control.

## Install a release

Download the appropriate archive from the repository's **Releases** page and
extract the complete folder to a writable location. Do not run from inside the
archive.

On Windows:

- Generator package: double-click `Prisma.exe`.
- Suite package: double-click `Prisma Generator.exe` or
  `Prisma Calibration.exe`.

On Linux:

- Generator package: run `./Prisma` from the extracted `Prisma` folder.
- Suite package: run `./Prisma Generator` or `./Prisma Calibration` from the
  extracted `Prisma Suite` folder.

Keep the console window open while using Prisma. The application opens its
browser interface automatically when possible and otherwise prints the local
URL. Release users do not need Python, an IDE, CAD software, or a separate
dependency installation.

Detailed guides are maintained in the separate
[Prisma documentation repository](https://github.com/CatfoodKingdom/prisma-docs).

## Data and model libraries

Portable releases keep visible data beside the executable:

- `Generator/Images` — source images;
- `Generator/Exports` — completed print files;
- `Generator/Model Libraries` — installed published model bundles;
- `Generator/Workspace` — settings, runs, caches, and logs;
- `Calibration/Inbox` and `Calibration/Output` — calibration imports/exports;
- `Calibration/Workspace` — the private calibration database and assets.

Install or update a color-model bundle through **Model Library → Manage** in
Generator. Selecting a different valid bundle takes effect after the restart
shown by the application. Never merge model files from different bundles.

Use Calibration's **Backup / Restore** workflow to migrate its workspace. Keep
important backups outside the live Prisma folder, and do not copy individual
database or asset files between installations.

## Run from source

Source operation requires Python 3.12 and
[`uv`](https://docs.astral.sh/uv/). Always provide an external `--app-root` so
runtime data cannot enter the checkout.

Generator on Linux or macOS:

```bash
uv sync --frozen --only-group generator-runtime --no-install-project
.venv/bin/python -m Prisma.launcher --app-root "$HOME/PrismaRuntime"
```

Generator on Windows PowerShell:

```powershell
uv sync --frozen --only-group generator-runtime --no-install-project
.\.venv\Scripts\python.exe -m Prisma.launcher `
  --app-root (Join-Path $HOME "PrismaRuntime")
```

For the full source Suite, install both runtime groups and launch both apps
against the same external root:

```bash
uv sync --frozen \
  --only-group generator-runtime \
  --only-group calibration-runtime \
  --no-install-project
.venv/bin/python -m Prisma.launcher --app-root "$HOME/PrismaRuntime"
.venv/bin/python -m Prisma.calibration_launcher --app-root "$HOME/PrismaRuntime"
```

Without an installed model bundle, Generator intentionally starts in Library
Recovery mode so **Manage** remains available.

## Platform status

- Windows 11 x64 packages are built and smoke-tested on Windows.
- Linux x86-64 packages are currently built on Ubuntu 24.04 under WSL2 and
  require glibc 2.38 or newer. Native desktop integration has not yet been
  verified on a separate Linux machine.
- macOS source compatibility has been statically audited, but no macOS package
  has been built or tested. macOS support is not currently claimed.

Copyright © 2026 Catfood Kingdom. Prisma source code is licensed under GPLv3;
see [LICENSE](LICENSE). Dependency
and non-code asset terms are described in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[ASSET_LICENSES.md](ASSET_LICENSES.md).
