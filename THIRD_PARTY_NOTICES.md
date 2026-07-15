# Third-party notices

Prisma depends on third-party software. Those components retain their own
copyrights and licenses; GPLv3 applies to Prisma's project-authored source, not
as a replacement for dependency licenses.

The exact resolved versions are recorded in `uv.lock`. The principal runtime
and packaging components are:

| Component | Primary license or license family |
| --- | --- |
| CPython | Python Software Foundation License 2.0 |
| FastAPI, Pydantic, pydantic-core, AnyIO, h11 | MIT |
| Uvicorn, Starlette, Click | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| NumPy | BSD-3-Clause with separately licensed bundled components |
| pandas | BSD-3-Clause |
| SciPy | BSD-3-Clause; binary wheels may include BSD-licensed OpenBLAS |
| scikit-image | BSD-3-Clause with documented BSD/MIT components |
| Pillow | HPND/MIT-CMU family; bundled image codecs retain their own terms |
| opencv-python-headless / OpenCV | Apache-2.0 |
| ImageIO | BSD-2-Clause |
| tifffile, lazy-loader, NetworkX | BSD family |
| trimesh | MIT |
| Shapely | BSD-3-Clause; binary wheels may include LGPL-licensed GEOS |
| mapbox-earcut | ISC |
| xxHash / python-xxhash | BSD-2-Clause |
| ExifRead | BSD-3-Clause |
| rawpy | MIT; binary wheels include LibRaw under its upstream terms |
| packaging | Apache-2.0 or BSD-2-Clause |
| python-dateutil, six | Apache/BSD and MIT respectively |
| PyInstaller | GPL-2.0 with the PyInstaller bootloader exception; selected files are Apache-2.0 |
| pyinstaller-hooks-contrib | Apache-2.0 and GPL-2.0-or-later components |
| altgraph, pefile | MIT |
| pywin32-ctypes, colorama | BSD-3-Clause |
| tzdata | Apache-2.0 |

Release packages must carry the license/notices files collected from the exact
build environment, including notices for native libraries embedded in wheels.
This summary is an audit index, not a substitute for those upstream texts.

PyInstaller documents that its bootloader exception permits distributing the
generated executable under the application's license, subject to the licenses
of bundled dependencies. See <https://pyinstaller.org/en/stable/license.html>.

No endorsement by any dependency author or project is implied.
