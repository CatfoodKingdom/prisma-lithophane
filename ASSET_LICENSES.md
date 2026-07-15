# Non-code assets and data

The GPLv3 license in `LICENSE` covers Prisma's project-authored source code. It
does not automatically establish redistribution terms for independently
created model/data assets.

Catfood Kingdom owns and authorizes redistribution of the unmodified Prisma
Standard Model Library with official Prisma packages that include this notice,
including the current private friend-test packages. Copyright is retained by
Catfood Kingdom; this authorization does not grant a separate license to
extract, modify, or redistribute the model library apart from those Prisma
packages unless another written grant accompanies it.

The following independently maintained test/reference assets are not included
in the GitHub source staging tree or release packages and remain outside that
authorization:

- `Prisma/lib/photo_stack_model/bundles/runtime_bundle.json`, a
  calibration-derived reference/test model;
- photographic preprocessing fixtures under
  `tests/generator/preprocessing/fixtures/wing_d/`.

These items must not be treated as GPL-licensed merely because they appear near
GPL-covered source. The first public source staging tree excludes the
photographic fixture directory and the calibration-derived reference bundle.

The user documentation vault is maintained separately in
<https://github.com/CatfoodKingdom/prisma-docs> and is not part of the main
Prisma source repository.
