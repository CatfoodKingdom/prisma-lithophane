"""Auto-discovered preprocessing operators.

Operator modules placed here are imported in lexicographic order by
`Prisma/generator/pipeline/registry.py:_ensure_registry_populated()`. Each
module is expected to define exactly one class decorated with
`@register_preprocessing` (PreprocessingModule subclass). Helper modules MUST
NOT live here — keep them at the `preprocessing/` package root.
"""
