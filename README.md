# Prisma

Prisma is a local application for making full-color lithophanes with an FDM
printer. It runs on your computer and serves its interface only on the local
loopback address; it is not a hosted web service, and uploaded images stay on
your machine.

Prisma has two applications:

- **Prisma Generator** converts an image into printable 3MF/STL files, color
  layers, and filament-swap instructions using a published color-model bundle.
  Its **Theme** control supports System, Light, and Dark appearances; System
  follows the operating-system preference.
- **Prisma Calibration** measures printed filament samples, fits private
  working models, and publishes complete immutable model bundles for Generator.
  Calibration currently uses a light interface only.

The applications do not share live working data. Generator only consumes a
published bundle selected through its Model Library control.

**Want to install or test Prisma?** [Download the latest release from the
Releases page](https://github.com/CatfoodKingdom/prisma-lithophane/releases).

## Install a release

Download the appropriate archive from the repository's
[**Releases** page](https://github.com/CatfoodKingdom/prisma-lithophane/releases)
and extract the complete folder to a writable location. Do not run from inside
the archive.

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

Detailed guides are available on the
[Prisma documentation site](https://catfoodkingdom.github.io/prisma-docs/).

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

Generator stores its Theme choice in the current browser. The control is in
the right-side utility group beside **Clear Temp Files**. Choosing **System**
updates the interface when the operating-system color scheme changes; explicit
Light or Dark choices remain fixed.

Use Calibration's **Backup / Restore** workflow to migrate its workspace. Keep
important backups outside the live Prisma folder, and do not copy individual
database or asset files between installations.

## Run from source

"Running from source" means downloading Prisma's editable project files and
using Python to start them instead of running a prebuilt `.exe`. It is mainly
for developers and people helping diagnose a problem. If you simply want to
use or test Prisma, download a package from
[Releases](https://github.com/CatfoodKingdom/prisma-lithophane/releases) instead.

### Before you begin

1. Download the source with **Code → Download ZIP** on this repository page,
   then extract the ZIP. You may use `git clone` instead if you already know
   Git.
2. Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), the
   tool that obtains the correct Python version and installs Prisma's required
   Python packages.
3. Open a terminal in the extracted `prisma-lithophane` folder. On Windows,
   open the folder in File Explorer, right-click its empty background, and
   choose **Open in Terminal**.

The first setup can take several minutes. It downloads Python 3.12 when needed
and creates a private `.venv` folder containing Prisma's dependencies. It does
not install Prisma as a system-wide application.

### Start Generator on Windows

Paste these commands into PowerShell, one line at a time:

```powershell
uv sync --frozen --only-group generator-runtime --no-install-project
New-Item -ItemType Directory -Force "$HOME\PrismaRuntime"
.\.venv\Scripts\python.exe -m Prisma.launcher --app-root "$HOME\PrismaRuntime"
```

### Start Generator on Linux or macOS

Paste these commands into the terminal, one line at a time:

```bash
uv sync --frozen --only-group generator-runtime --no-install-project
mkdir -p "$HOME/PrismaRuntime"
.venv/bin/python -m Prisma.launcher --app-root "$HOME/PrismaRuntime"
```

Prisma normally opens its browser interface automatically. If it does not,
open the local `http://127.0.0.1:...` address printed in the terminal. Keep the
terminal open while using Prisma. Close it, or press **Ctrl+C**, to stop Prisma.

`PrismaRuntime` is a separate data folder inside your home folder. The commands
above create it before starting Prisma. It
holds images, exports, settings, and installed models so that personal data
does not get mixed into the downloaded source files.

On the first source launch, Generator may show **Library Recovery** because the
source download deliberately contains no model library. Select **Manage** and
install the
[Prisma Standard Model Library](https://github.com/CatfoodKingdom/prisma-lithophane/releases/download/v0.1.0/Prisma-Model-Library-Prisma-Standard-Model-Library-2026.07.12-12d373ee-2837-4822-a04e-abd0913c48cc.zip),
then follow the restart prompt.

### Start the full Generator + Calibration Suite

Run this setup command once:

```powershell
uv sync --frozen --only-group generator-runtime --only-group calibration-runtime --no-install-project
```

Then start Generator with the command shown above. To use Calibration at the
same time, open a second terminal in the same source folder and run:

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m Prisma.calibration_launcher --app-root "$HOME\PrismaRuntime"
```

On Linux or macOS:

```bash
.venv/bin/python -m Prisma.calibration_launcher --app-root "$HOME/PrismaRuntime"
```

If PowerShell says `uv` is not recognized after installation, close and reopen
the terminal. If Prisma says its port or workspace is already in use, close any
other Prisma terminal and try again.

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
