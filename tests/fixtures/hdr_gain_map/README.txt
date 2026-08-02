# HDR gain-map fixtures

`apple_gain_map_mpo.jpg.b64` is a deterministic synthetic test image generated
with Pillow 12.1.1 from an 8 x 6 solid-color RGB primary and a 4 x 3 solid-color
grayscale auxiliary. The auxiliary XMP identifies an Apple HDR gain map. It
contains no user or camera data and is dedicated to this repository's tests.

The base64 form keeps the binary fixture reviewable and avoids source-control
transformations. Tests decode it without modifying this file.
