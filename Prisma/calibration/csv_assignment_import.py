from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path
from typing import Any

from models import Sample
from sample_mutations import recompute_sample_status


TEMPLATE_COLUMNS = ("Sample ID", "Sample Image", "Blank Image", "Orientation")
TEMPLATE_CSV = ",".join(TEMPLATE_COLUMNS) + "\n"
ORIENTATION_MAP = {
    "U": (0, "U"),
    "T": (0, "U"),
    "R": (1, "R"),
    "D": (2, "D"),
    "B": (2, "D"),
    "L": (3, "L"),
}


class CsvAssignmentError(ValueError):
    pass


def upload_digest(csv_bytes: bytes) -> str:
    return hashlib.sha256(csv_bytes).hexdigest()


def validate_assignment_csv(store: Any, csv_bytes: bytes) -> dict[str, Any]:
    rows, warnings = _parse_csv(csv_bytes)
    samples = store.list_samples()
    images = store.list_images()
    blanks = _list_blank_assets(store)
    extraction_sample_ids = _extraction_sample_ids(store)

    sample_resolver = _SampleResolver(samples)
    image_resolver = _ImageResolver(images)
    blank_resolver = _BlankResolver(blanks, image_resolver)
    assigned_image_ids = _assigned_image_asset_ids(store, samples)

    valid_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    row_contexts: list[dict[str, Any]] = []

    for parsed in rows:
        row_errors: list[str] = []
        context: dict[str, Any] = {
            "row_number": parsed["row_number"],
            "input": parsed["values"],
        }
        values = parsed["values"]

        sample = _resolve_sample(sample_resolver, values.get("sample_id", ""), row_errors)
        if sample is not None:
            context["sample_id"] = sample.sample_id
            row_errors.extend(_sample_eligibility_errors(store, sample, extraction_sample_ids))

        image = _resolve_image(image_resolver, values.get("sample_image", ""), row_errors)
        if image is not None:
            context["sample_image_asset_id"] = image["image_asset_id"]
            context["sample_image"] = image["filename"]
            if not image.get("path_exists"):
                row_errors.append(f"Sample image '{image['filename']}' is missing from managed storage.")
            if image.get("ignored"):
                row_errors.append(f"Sample image '{image['filename']}' is ignored.")
            if image["image_asset_id"] in assigned_image_ids:
                row_errors.append(
                    f"Sample image '{image['filename']}' is already assigned to "
                    f"{', '.join(sorted(assigned_image_ids[image['image_asset_id']]))}."
                )

        blank, pending_blank = _resolve_blank(blank_resolver, values.get("blank_image", ""), row_errors)
        if blank is not None:
            context["blank_id"] = blank["blank_id"]
            context["blank_image_asset_id"] = blank["image_asset_id"]
            context["blank_image"] = blank["filename"]
            if not blank.get("path_exists"):
                row_errors.append(f"Blank '{blank['blank_id']}' is missing from managed storage.")
        elif pending_blank is not None:
            context["blank_registration_required"] = True
            context["blank_image_asset_id"] = pending_blank["image_asset_id"]
            context["blank_image"] = pending_blank["filename"]
            if not pending_blank.get("path_exists"):
                row_errors.append(f"Blank image '{pending_blank['filename']}' is missing from managed storage.")
            if pending_blank.get("ignored"):
                row_errors.append(f"Blank image '{pending_blank['filename']}' is ignored.")

        orientation = _resolve_orientation(values.get("orientation", ""), row_errors)
        if orientation is not None:
            context["orientation_rots"] = orientation[0]
            context["orientation"] = orientation[1]

        row_contexts.append({**context, "errors": row_errors})

    _add_cross_row_errors(row_contexts)

    for context in row_contexts:
        if context["errors"]:
            error_rows.append(_preview_row(context, valid=False))
        else:
            valid_rows.append(_preview_row(context, valid=True))

    return {
        "total_rows": len(rows),
        "valid_count": len(valid_rows),
        "error_count": len(error_rows),
        "warning_count": len(warnings),
        "valid_rows": valid_rows,
        "error_rows": error_rows,
        "pending_blank_registrations": _pending_blank_registrations(valid_rows),
        "warnings": warnings,
    }


def commit_assignment_rows(
    store: Any,
    row_specs: list[dict[str, Any]],
    *,
    register_unregistered_blanks: bool = False,
) -> dict[str, Any]:
    samples_by_id = {sample.sample_id: sample for sample in store.list_samples()}
    images_by_id = {image["image_asset_id"]: image for image in store.list_images()}
    blanks_by_id = {blank["blank_id"]: blank for blank in _list_blank_assets(store)}
    assigned_image_ids = _assigned_image_asset_ids(store, samples_by_id.values())
    extraction_sample_ids = _extraction_sample_ids(store)
    registered_by_image_id = {
        str(blank.get("image_asset_id") or ""): str(blank.get("blank_id") or "")
        for blank in blanks_by_id.values()
        if blank.get("image_asset_id") and blank.get("blank_id")
    }

    stale_errors: list[dict[str, Any]] = []
    samples_to_save: list[Sample] = []
    seen_samples: set[str] = set()
    seen_images: set[str] = set()
    registered_blanks: list[dict[str, str]] = []
    pending_image_ids = sorted({
        str(spec.get("blank_image_asset_id") or "")
        for spec in row_specs
        if spec.get("blank_registration_required") and spec.get("blank_image_asset_id")
    })

    if pending_image_ids and not register_unregistered_blanks:
        raise CsvAssignmentError("CSV assignment requires blank registration confirmation.")

    def collect_stale_errors(
        specs: list[dict[str, Any]],
        *,
        allow_pending_blanks: bool,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        local_seen_samples: set[str] = set()
        local_seen_images: set[str] = set()
        for spec in specs:
            row_errors: list[str] = []
            sample_id = spec["sample_id"]
            image_id = spec["sample_image_asset_id"]
            blank_id = spec.get("blank_id") or ""
            pending_blank = bool(spec.get("blank_registration_required"))
            sample = samples_by_id.get(sample_id)
            image = images_by_id.get(image_id)
            blank = blanks_by_id.get(blank_id)
            pending_blank_image = images_by_id.get(str(spec.get("blank_image_asset_id") or "")) if pending_blank else None

            if sample_id in local_seen_samples:
                row_errors.append(f"Sample '{sample_id}' appears more than once.")
            local_seen_samples.add(sample_id)
            if image_id in local_seen_images:
                row_errors.append(f"Sample image '{spec.get('sample_image') or image_id}' appears more than once.")
            local_seen_images.add(image_id)

            if sample is None:
                row_errors.append(f"Sample '{sample_id}' no longer exists.")
            else:
                row_errors.extend(_sample_eligibility_errors(store, sample, extraction_sample_ids))

            if image is None:
                row_errors.append(f"Sample image '{spec.get('sample_image') or image_id}' no longer exists.")
            else:
                if not _path_exists(image.get("path")):
                    row_errors.append(f"Sample image '{image['filename']}' is missing from managed storage.")
                if image.get("ignored"):
                    row_errors.append(f"Sample image '{image['filename']}' is ignored.")
                assigned_to = [sid for sid in assigned_image_ids.get(image_id, []) if sid != sample_id]
                if assigned_to:
                    row_errors.append(
                        f"Sample image '{image['filename']}' is now assigned to {', '.join(sorted(assigned_to))}."
                    )

            if pending_blank and allow_pending_blanks:
                if pending_blank_image is None:
                    row_errors.append(f"Blank image '{spec.get('blank_image') or spec.get('blank_image_asset_id')}' no longer exists.")
                else:
                    if not _path_exists(pending_blank_image.get("path")):
                        row_errors.append(f"Blank image '{pending_blank_image['filename']}' is missing from managed storage.")
                    if pending_blank_image.get("ignored"):
                        row_errors.append(f"Blank image '{pending_blank_image['filename']}' is ignored.")
                    if image is not None and image_id == pending_blank_image["image_asset_id"]:
                        row_errors.append("The sample image is also the selected blank image.")
            else:
                if blank is None:
                    row_errors.append(f"Blank '{blank_id}' no longer exists.")
                elif not blank.get("path_exists"):
                    row_errors.append(f"Blank '{blank_id}' is missing from managed storage.")
                if image is not None and blank is not None and image_id == blank["image_asset_id"]:
                    row_errors.append("The sample image is also registered as the selected blank.")

            if row_errors:
                errors.append({
                    "row_number": spec.get("row_number"),
                    "sample_id": sample_id,
                    "errors": row_errors,
                })
        return errors

    stale_errors.extend(collect_stale_errors(row_specs, allow_pending_blanks=True))
    if stale_errors:
        raise CsvAssignmentError("CSV assignment preview is stale.")

    normalized_specs: list[dict[str, Any]] = []
    for spec in row_specs:
        normalized = dict(spec)
        if normalized.get("blank_registration_required"):
            image_id = str(normalized.get("blank_image_asset_id") or "")
            blank_id = registered_by_image_id.get(image_id)
            if blank_id:
                normalized["blank_id"] = blank_id
        normalized_specs.append(normalized)

    for spec in normalized_specs:
        row_errors: list[str] = []
        sample_id = spec["sample_id"]
        image_id = spec["sample_image_asset_id"]
        blank_id = spec["blank_id"]
        sample = samples_by_id.get(sample_id)
        image = images_by_id.get(image_id)
        blank = blanks_by_id.get(blank_id)

        if sample_id in seen_samples:
            row_errors.append(f"Sample '{sample_id}' appears more than once.")
        seen_samples.add(sample_id)
        if image_id in seen_images:
            row_errors.append(f"Sample image '{spec.get('sample_image') or image_id}' appears more than once.")
        seen_images.add(image_id)

        if sample is None:
            row_errors.append(f"Sample '{sample_id}' no longer exists.")
        else:
            row_errors.extend(_sample_eligibility_errors(store, sample, extraction_sample_ids))

        if image is None:
            row_errors.append(f"Sample image '{spec.get('sample_image') or image_id}' no longer exists.")
        else:
            if not _path_exists(image.get("path")):
                row_errors.append(f"Sample image '{image['filename']}' is missing from managed storage.")
            if image.get("ignored"):
                row_errors.append(f"Sample image '{image['filename']}' is ignored.")
            assigned_to = [sid for sid in assigned_image_ids.get(image_id, []) if sid != sample_id]
            if assigned_to:
                row_errors.append(
                    f"Sample image '{image['filename']}' is now assigned to {', '.join(sorted(assigned_to))}."
                )

        pending_blank = bool(spec.get("blank_registration_required"))
        if not pending_blank:
            if blank is None:
                row_errors.append(f"Blank '{blank_id}' no longer exists.")
            elif not blank.get("path_exists"):
                row_errors.append(f"Blank '{blank_id}' is missing from managed storage.")

        if image is not None and blank is not None and image_id == blank["image_asset_id"]:
            row_errors.append("The sample image is also registered as the selected blank.")

        if row_errors:
            stale_errors.append({
                "row_number": spec.get("row_number"),
                "sample_id": sample_id,
                "errors": row_errors,
            })
            continue

        assert sample is not None
        sample.assigned_image = image_id
        sample.assigned_blank_id = blank_id
        sample.orientation_rots = int(spec["orientation_rots"])
        recompute_sample_status(sample)
        samples_to_save.append(sample)

    if stale_errors:
        raise CsvAssignmentError("CSV assignment preview is stale.")

    if samples_to_save:
        if pending_image_ids:
            save_with_registrations = getattr(store, "save_samples_with_blank_registrations", None)
            if not callable(save_with_registrations):
                raise CsvAssignmentError(
                    "This backend cannot atomically register blanks with sample assignments."
                )
            blank_images_by_sample = {
                str(spec["sample_id"]): str(spec["blank_image_asset_id"])
                for spec in normalized_specs
                if spec.get("blank_registration_required")
            }
            result = save_with_registrations(
                samples_to_save,
                blank_image_asset_ids_by_sample=blank_images_by_sample,
            )
            registered_by_image_id.update(result["blank_ids_by_image_asset_id"])
            registered_blanks = [
                {
                    "blank_id": blank.blank_id,
                    "image_asset_id": image_id,
                    "filename": blank.original_filename,
                }
                for blank in result["registered_blanks"]
                for image_id, blank_id in result["blank_ids_by_image_asset_id"].items()
                if blank_id == blank.blank_id
            ]
            for spec in normalized_specs:
                if spec.get("blank_registration_required"):
                    spec["blank_id"] = registered_by_image_id[str(spec["blank_image_asset_id"])]
        else:
            store.save_samples(samples_to_save)

    return {
        "ok": True,
        "committed_count": len(samples_to_save),
        "registered_blank_count": len(registered_blanks),
        "registered_blanks": registered_blanks,
        "committed_rows": [
            {
                "row_number": spec.get("row_number"),
                "sample_id": spec.get("sample_id"),
                "sample_image": spec.get("sample_image"),
                "blank_id": spec.get("blank_id"),
                "orientation": spec.get("orientation"),
            }
            for spec in normalized_specs
        ],
    }


def _parse_csv(csv_bytes: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not csv_bytes:
        raise CsvAssignmentError("CSV file is empty.")
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvAssignmentError("CSV must be UTF-8 text.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvAssignmentError("CSV is missing a header row.")

    normalized_headers: dict[str, str] = {}
    warnings: list[dict[str, Any]] = []
    for header in reader.fieldnames:
        key = _normalize_header(header or "")
        if key in normalized_headers:
            raise CsvAssignmentError(f"CSV contains duplicate column for '{header}'.")
        normalized_headers[key] = header or ""

    required = {
        "sampleid": "sample_id",
        "sampleimage": "sample_image",
        "blankimage": "blank_image",
        "orientation": "orientation",
    }
    missing = [label for key, label in required.items() if key not in normalized_headers]
    if missing:
        raise CsvAssignmentError(f"CSV is missing required column(s): {', '.join(missing)}.")

    expected_keys = set(required)
    for key, original in normalized_headers.items():
        if key not in expected_keys:
            warnings.append({
                "message": f"Ignoring extra column '{original}'.",
                "column": original,
            })

    parsed_rows: list[dict[str, Any]] = []
    for index, row in enumerate(reader, start=2):
        values = {
            out_key: str(row.get(normalized_headers[in_key]) or "").strip()
            for in_key, out_key in required.items()
        }
        if not any(values.values()):
            continue
        parsed_rows.append({"row_number": index, "values": values})
    if not parsed_rows:
        raise CsvAssignmentError("CSV contains no assignment rows.")
    return parsed_rows, warnings


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.strip().lower())


def _is_path_like(value: str) -> bool:
    return "/" in value or "\\" in value or ":" in value


def _stem(value: str) -> str:
    return Path(value).stem.lower()


def _path_exists(path_value: Any) -> bool:
    try:
        return bool(path_value) and Path(str(path_value)).exists()
    except (OSError, ValueError):
        return False


class _SampleResolver:
    def __init__(self, samples: list[Sample]) -> None:
        self.by_id = {sample.sample_id: sample for sample in samples}
        self.by_number: dict[int, list[Sample]] = {}
        for sample in samples:
            match = re.fullmatch(r"exp-(\d+)", sample.sample_id)
            if match:
                self.by_number.setdefault(int(match.group(1)), []).append(sample)

    def resolve(self, value: str) -> Sample | None:
        if value in self.by_id:
            return self.by_id[value]
        if value.isdigit():
            matches = self.by_number.get(int(value), [])
            if len(matches) == 1:
                return matches[0]
        return None

    def numeric_matches(self, value: str) -> int:
        if not value.isdigit():
            return 0
        return len(self.by_number.get(int(value), []))


class _ImageResolver:
    def __init__(self, images: list[dict[str, Any]]) -> None:
        self.by_id = {str(image["image_asset_id"]): _image_info(image) for image in images}
        self.by_exact: dict[str, list[dict[str, Any]]] = {}
        self.by_stem: dict[str, list[dict[str, Any]]] = {}
        for image in self.by_id.values():
            self.by_exact.setdefault(str(image["filename"]).lower(), []).append(image)
            self.by_stem.setdefault(_stem(str(image["filename"])), []).append(image)

    def resolve(self, value: str) -> tuple[dict[str, Any] | None, str | None]:
        if not value:
            return None, "missing"
        if _is_path_like(value):
            return None, "path"
        by_id = self.by_id.get(value)
        if by_id is not None:
            return by_id, None
        exact = self.by_exact.get(value.lower(), [])
        if len(exact) > 1:
            return None, "ambiguous_exact"
        if len(exact) == 1:
            return exact[0], None
        stem = self.by_stem.get(_stem(value), [])
        if len(stem) > 1:
            return None, "ambiguous_stem"
        if len(stem) == 1:
            return stem[0], None
        return None, "missing"


class _BlankResolver:
    def __init__(self, blanks: list[dict[str, Any]], image_resolver: _ImageResolver) -> None:
        self.image_resolver = image_resolver
        self.by_id = {str(blank["blank_id"]).lower(): blank for blank in blanks}
        self.by_exact: dict[str, list[dict[str, Any]]] = {}
        self.by_stem: dict[str, list[dict[str, Any]]] = {}
        for blank in blanks:
            self.by_exact.setdefault(str(blank["filename"]).lower(), []).append(blank)
            self.by_stem.setdefault(_stem(str(blank["filename"])), []).append(blank)

    def resolve(self, value: str) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
        if not value:
            return None, "missing", None
        if _is_path_like(value):
            return None, "path", None
        by_id = self.by_id.get(value.lower())
        if by_id is not None:
            return by_id, None, None
        exact = self.by_exact.get(value.lower(), [])
        if len(exact) > 1:
            return None, "ambiguous_exact", None
        if len(exact) == 1:
            return exact[0], None, None
        stem = self.by_stem.get(_stem(value), [])
        if len(stem) > 1:
            return None, "ambiguous_stem", None
        if len(stem) == 1:
            return stem[0], None, None
        image, image_error = self.image_resolver.resolve(value)
        if image is not None:
            return None, "unregistered_image", image
        return None, image_error or "missing", None


def _image_info(image: dict[str, Any]) -> dict[str, Any]:
    info = dict(image)
    info["filename"] = str(image.get("filename") or "")
    info["image_asset_id"] = str(image.get("image_asset_id") or "")
    info["path_exists"] = _path_exists(image.get("path"))
    return info


def _list_blank_assets(store: Any) -> list[dict[str, Any]]:
    if hasattr(store, "list_blank_assets"):
        return store.list_blank_assets()
    rows = []
    for blank in store.list_blanks():
        path = getattr(blank, "storage_path", "") or ""
        rows.append({
            "blank_id": blank.blank_id,
            "image_asset_id": "",
            "filename": blank.original_filename,
            "path": path,
            "path_exists": _path_exists(path),
        })
    return rows


def _extraction_sample_ids(store: Any) -> set[str]:
    ids: set[str] = set()
    for sample in store.list_samples():
        if sample.measurements is not None:
            ids.add(sample.sample_id)
            continue
        if hasattr(store, "get_extraction_result"):
            try:
                if store.get_extraction_result(sample.sample_id) is not None:
                    ids.add(sample.sample_id)
            except Exception:
                pass
    return ids


def _assigned_image_asset_ids(store: Any, samples: Any) -> dict[str, list[str]]:
    if hasattr(store, "list_sample_image_asset_assignments"):
        assigned: dict[str, list[str]] = {}
        for row in store.list_sample_image_asset_assignments():
            image_asset_id = str(row.get("image_asset_id") or "")
            sample_id = str(row.get("sample_id") or "")
            if image_asset_id and sample_id:
                assigned.setdefault(image_asset_id, []).append(sample_id)
        return assigned

    images = {image["image_asset_id"]: image for image in store.list_images()}
    by_filename = {str(image["filename"]).lower(): image for image in images.values()}
    assigned: dict[str, list[str]] = {}
    for sample in samples:
        value = sample.assigned_image
        if not value:
            continue
        image = images.get(value) or by_filename.get(str(value).lower())
        if image is None:
            continue
        assigned.setdefault(image["image_asset_id"], []).append(sample.sample_id)
    return assigned


def _sample_eligibility_errors(store: Any, sample: Sample, extraction_sample_ids: set[str]) -> list[str]:
    errors = []
    if sample.assigned_image:
        errors.append(f"Sample '{sample.sample_id}' already has a sample image.")
    if sample.assigned_blank_id:
        errors.append(f"Sample '{sample.sample_id}' already has a blank.")
    if sample.orientation_rots is not None:
        errors.append(f"Sample '{sample.sample_id}' already has an orientation.")
    if (sample.processing_status or "unassigned") != "unassigned":
        errors.append(f"Sample '{sample.sample_id}' status is '{sample.processing_status}'.")
    if sample.measurements is not None or sample.sample_id in extraction_sample_ids:
        errors.append(f"Sample '{sample.sample_id}' already has extraction results.")
    return errors


def _resolve_sample(resolver: _SampleResolver, value: str, errors: list[str]) -> Sample | None:
    if not value:
        errors.append("Sample ID is required.")
        return None
    sample = resolver.resolve(value)
    if sample is not None:
        return sample
    if value.isdigit() and resolver.numeric_matches(value) > 1:
        errors.append(f"Sample ID '{value}' is ambiguous.")
    else:
        errors.append(f"Sample ID '{value}' was not found.")
    return None


def _resolve_image(
    resolver: _ImageResolver, value: str, errors: list[str]
) -> dict[str, Any] | None:
    image, error = resolver.resolve(value)
    if image is not None:
        return image
    messages = {
        "missing": "Sample Image is required." if not value else f"Sample image '{value}' was not found.",
        "path": f"Sample image '{value}' must be a filename, not a path.",
        "ambiguous_exact": f"Sample image '{value}' is ambiguous.",
        "ambiguous_stem": f"Sample image '{value}' is ambiguous.",
    }
    errors.append(messages.get(error or "missing", f"Sample image '{value}' was not found."))
    return None


def _resolve_blank(
    resolver: _BlankResolver, value: str, errors: list[str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    blank, error, pending_image = resolver.resolve(value)
    if blank is not None:
        return blank, None
    if error == "unregistered_image" and pending_image is not None:
        return None, pending_image
    messages = {
        "missing": "Blank Image is required." if not value else f"Blank image '{value}' was not found.",
        "path": f"Blank image '{value}' must be a filename or blank id, not a path.",
        "ambiguous_exact": f"Blank image '{value}' is ambiguous.",
        "ambiguous_stem": f"Blank image '{value}' is ambiguous.",
        "unregistered_image": f"Image '{value}' exists but is not registered as a blank.",
    }
    errors.append(messages.get(error or "missing", f"Blank image '{value}' was not found."))
    return None, None


def _pending_blank_registrations(valid_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in valid_rows:
        if not row.get("blank_registration_required"):
            continue
        image_id = str(row.get("blank_image_asset_id") or "")
        if not image_id:
            continue
        entry = grouped.setdefault(image_id, {
            "image_asset_id": image_id,
            "filename": row.get("blank_image") or image_id,
            "uses": 0,
        })
        entry["uses"] += 1
    return sorted(grouped.values(), key=lambda item: str(item.get("filename") or "").lower())


def _resolve_orientation(value: str, errors: list[str]) -> tuple[int, str] | None:
    if not value:
        errors.append("Orientation is required.")
        return None
    resolved = ORIENTATION_MAP.get(value.strip().upper())
    if resolved is None:
        errors.append(f"Orientation '{value}' is invalid. Use U, D, L, or R.")
        return None
    return resolved


def _add_cross_row_errors(contexts: list[dict[str, Any]]) -> None:
    sample_rows: dict[str, list[dict[str, Any]]] = {}
    image_rows: dict[str, list[dict[str, Any]]] = {}
    image_blank_rows: dict[str, list[dict[str, Any]]] = {}

    for context in contexts:
        if "sample_id" in context:
            sample_rows.setdefault(context["sample_id"], []).append(context)
        if "sample_image_asset_id" in context:
            image_rows.setdefault(context["sample_image_asset_id"], []).append(context)
            image_blank_rows.setdefault(context["sample_image_asset_id"], []).append(context)
        if "blank_image_asset_id" in context:
            image_blank_rows.setdefault(context["blank_image_asset_id"], []).append(context)

    for sample_id, rows in sample_rows.items():
        if len(rows) > 1:
            row_numbers = ", ".join(str(row["row_number"]) for row in rows)
            for row in rows:
                row["errors"].append(f"Sample '{sample_id}' appears in multiple CSV rows: {row_numbers}.")

    for image_id, rows in image_rows.items():
        if len(rows) > 1:
            row_numbers = ", ".join(str(row["row_number"]) for row in rows)
            label = rows[0].get("sample_image") or image_id
            for row in rows:
                row["errors"].append(f"Sample image '{label}' appears in multiple CSV rows: {row_numbers}.")

    for asset_id, rows in image_blank_rows.items():
        has_sample = any(row.get("sample_image_asset_id") == asset_id for row in rows)
        has_blank = any(row.get("blank_image_asset_id") == asset_id for row in rows)
        if has_sample and has_blank:
            label = next((row.get("sample_image") or row.get("blank_image") for row in rows if row.get("sample_image_asset_id") == asset_id), asset_id)
            for row in rows:
                if row.get("sample_image_asset_id") == asset_id or row.get("blank_image_asset_id") == asset_id:
                    row["errors"].append(f"Image '{label}' is used as both a sample image and a blank.")


def _preview_row(context: dict[str, Any], *, valid: bool) -> dict[str, Any]:
    row = {
        "row_number": context["row_number"],
        "valid": valid,
        "sample_id": context.get("sample_id") or "",
        "sample_image": context.get("sample_image") or "",
        "sample_image_asset_id": context.get("sample_image_asset_id") or "",
        "blank_id": context.get("blank_id") or "",
        "blank_image": context.get("blank_image") or "",
        "blank_image_asset_id": context.get("blank_image_asset_id") or "",
        "blank_registration_required": bool(context.get("blank_registration_required")),
        "orientation": context.get("orientation") or "",
        "orientation_rots": context.get("orientation_rots"),
        "errors": context.get("errors", []),
    }
    if valid:
        row["commit_spec"] = {
            "row_number": row["row_number"],
            "sample_id": row["sample_id"],
            "sample_image_asset_id": row["sample_image_asset_id"],
            "sample_image": row["sample_image"],
            "blank_id": row["blank_id"],
            "blank_image_asset_id": row["blank_image_asset_id"],
            "blank_image": row["blank_image"],
            "blank_registration_required": row["blank_registration_required"],
            "orientation_rots": row["orientation_rots"],
            "orientation": row["orientation"],
        }
    return row
