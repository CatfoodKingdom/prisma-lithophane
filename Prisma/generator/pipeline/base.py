# lithophane_generator/pipeline/base.py
"""Abstract base classes for pipeline modules."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from preprocessing.types import (
        ColorDomain,
        ContextKey,
        PreprocessingContext,
        PreprocessingResult,
    )

# Progress callback: callable that receives a progress dict
ProgressCallback = Optional[Callable[[dict], None]]


@dataclass
class ParamDef:
    """Self-describing parameter definition for a pipeline module.

    Core fields describe the parameter value. UI fields (unit, tooltip, group,
    show_when, order) tell the frontend renderer how to display it.
    """
    name: str
    label: str
    type: str               # "int", "float", "bool", "choice", "html", "computed_text"
    default: Any
    choices: List[Any] | None = None
    choice_labels: Dict[Any, str] | None = None
    min: float | None = None
    max: float | None = None
    description: str = ""
    # UI fields
    unit: str = ""                              # suffix: "mm", "px", "dE", "×"
    tooltip: str = ""                           # hover text (falls back to description)
    group: str = ""                             # visual group heading within module
    show_when: Dict[str, Any] | None = None     # {param_name: value} conditional visibility
    order: int = 0                              # display order within module

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict for API/UI consumption."""
        d = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "description": self.description,
            "unit": self.unit,
            "tooltip": self.tooltip,
            "group": self.group,
            "order": self.order,
        }
        if self.choices is not None:
            d["choices"] = self.choices
        if self.choice_labels is not None:
            d["choice_labels"] = self.choice_labels
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.show_when is not None:
            d["show_when"] = self.show_when
        return d


@dataclass(frozen=True)
class PresetDef:
    """Named product preset for a preprocessing module.

    Presets live beside the operator parameter contract so the application,
    saved-run inspector, and analysis tools cannot acquire independent copies
    of the values.
    """

    key: str
    label: str
    values: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool | None = None
    custom: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"key": self.key, "label": self.label}
        if self.values:
            result["values"] = dict(self.values)
        if self.enabled is not None:
            result["enabled"] = self.enabled
        if self.custom:
            result["custom"] = True
        return result


class PreprocessingModule(ABC):
    """Interface for image preprocessing operators (F1).

    Each operator declares its color-domain contract; the slot runner
    inserts conversions from `preprocessing/color_convert.py` between
    adjacent operators when their declared domains differ (R2-E).

    `order` controls position within the chain (R2-C). Active operators use
    the 100-199, 200-299, and 300-399 ranges. Ties on `order` resolve by lex
    import path (R3-C).

    `required_context` lists optional shared services, currently F2 palette
    metadata, that the runner must resolve before calling `apply()`. Operators
    that need none leave this empty.
    """
    name: str = ""
    description: str = ""
    params: Dict[str, ParamDef] = {}
    default_enabled: bool = False
    input_domain: "ColorDomain" = "srgb_u8"
    output_domain: "ColorDomain" = "srgb_u8"
    order: float = 1000.0
    required_context: FrozenSet["ContextKey"] = frozenset()
    display_label: str = ""
    display_tooltip: str = ""
    preset_control_label: str = ""
    default_preset: str | None = None
    presets: tuple[PresetDef, ...] = ()

    @abstractmethod
    def apply(
        self,
        image: "np.ndarray",
        *,
        context: "PreprocessingContext",
        progress: ProgressCallback,
    ) -> "PreprocessingResult":
        ...

    def describe(self) -> dict:
        descriptor = {
            "name": self.name,
            "description": self.description,
            "slot": "preprocessing",
            "default_enabled": self.default_enabled,
            "params": {k: v.to_dict() for k, v in self.params.items()},
            "input_domain": self.input_domain,
            "output_domain": self.output_domain,
            "order": self.order,
            "required_context": sorted(self.required_context),
        }
        if self.display_label or self.display_tooltip:
            descriptor["display"] = {
                "label": self.display_label or self.name,
                "tooltip": self.display_tooltip or self.description,
            }
        if self.presets:
            descriptor["preset_ui"] = {
                "control_label": self.preset_control_label or self.display_label or self.name,
                "default_preset": self.default_preset,
                "presets": [preset.to_dict() for preset in self.presets],
            }
        return descriptor
