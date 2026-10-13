#!/usr/bin/env python3
"""Remove URLs and hyperlinks from the user data export, then split oversized files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "user_data_export_extracted"
BACKUP_PARENT = Path(tempfile.gettempdir())
MAX_FILE_BYTES = 100_000_000  # GitHub's 100 MB per-file limit
PART_BYTES = 90 * 1024 * 1024
JSON_PART_RE = re.compile(r"\.json\.part-\d+$", re.IGNORECASE)

URL_PREFIX = (
    rb"(?:(?:https?|ftp|ftps|file):(?://|\\?/\\?/)"
    rb"|(?:mailto|tel|sms):|www\.)"
)
URL_RE = re.compile(
    rb"(?i)" + URL_PREFIX + rb"""[^\s<>"'`]+"""
)
INLINE_MARKDOWN_LINK_RE = re.compile(
    rb"(?i)!?\[([^\]\r\n]+)\]\(\s*<?"
    + URL_PREFIX
    + rb"""[^)\s>]*>?\s*\)"""
)
REFERENCE_MARKDOWN_LINK_RE = re.compile(
    rb"(?im)^[ \t]*\[[^\]\r\n]+\]:[ \t]*"
    + URL_PREFIX
    + rb"[^\r\n]*$"
)
HTML_ANCHOR_RE = re.compile(
    rb"(?is)<a\b[^>]*href\s*=\s*['\"]?"
    + URL_PREFIX
    + rb"""[^>]*>(.*?)</a\s*>"""
)
HTML_ANCHOR_TAG_RE = re.compile(rb"(?i)</?a\b[^>]*>")
EMPTY_MARKDOWN_LINK_RE = re.compile(rb"(!?)\[([^\]\r\n]*)\]\(\s*\)")


def remove_links(data: bytes) -> tuple[bytes, int]:
    """Remove URL targets while keeping ordinary link-label text."""
    removed = 0

    def keep_label(match: re.Match[bytes]) -> bytes:
        return match.group(1)

    data, count = INLINE_MARKDOWN_LINK_RE.subn(keep_label, data)
    removed += count
    data, count = REFERENCE_MARKDOWN_LINK_RE.subn(b"", data)
    removed += count
    data, count = HTML_ANCHOR_RE.subn(keep_label, data)
    removed += count
    data, count = URL_RE.subn(b"", data)
    removed += count
    data, count = HTML_ANCHOR_TAG_RE.subn(b"", data)
    removed += count
    data, count = EMPTY_MARKDOWN_LINK_RE.subn(keep_label, data)
    removed += count
    return data, removed


def is_json_part(path: Path) -> bool:
    return JSON_PART_RE.search(path.name) is not None


def is_json_file(path: Path) -> bool:
    return path.suffix.lower() == ".json" or is_json_part(path)


def sanitize_json_line(line: bytes) -> tuple[bytes, int]:
    """Rewrite only JSON string tokens, so URL removal cannot damage JSON syntax."""
    output = bytearray()
    removed = 0
    index = 0

    while index < len(line):
        if line[index] != 0x22:  # JSON quote
            output.append(line[index])
            index += 1
            continue

        start = index
        index += 1
        escaped = False
        while index < len(line):
            byte = line[index]
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:
                index += 1
                break
            index += 1
        else:
            raise ValueError(f"Unterminated JSON string while processing line in {ROOT}")

        token = line[start:index]
        value = json.loads(token.decode("utf-8"))
        cleaned_bytes, count = remove_links(
            value.encode("utf-8", errors="surrogatepass")
        )
        removed += count
        if count:
            cleaned_value = cleaned_bytes.decode("utf-8", errors="surrogatepass")
            output.extend(json.dumps(cleaned_value, ensure_ascii=True).encode("ascii"))
        else:
            output.extend(token)

    return bytes(output), removed


def sanitize_text_file(path: Path) -> int:
    temp_path = path.with_name(f".{path.name}.links-tmp")
    removed = 0
    try:
        with path.open("rb") as source, temp_path.open("wb") as target:
            for line in source:
                cleaned, count = sanitize_json_line(line)
                target.write(cleaned)
                removed += count
        shutil.copystat(path, temp_path)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return removed


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitize_xlsx(path: Path) -> dict[str, int]:
    """Remove URL text, hyperlink nodes, external-link references, and HYPERLINK formulas."""
    counts = {"urls": 0, "hyperlinks": 0, "external_links": 0, "formulas": 0}
    temp_path = path.with_name(f".{path.name}.links-tmp")

    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                name = info.filename
                if name.startswith("xl/externalLinks/"):
                    counts["external_links"] += 1
                    continue

                data = source.read(name)
                if name.endswith((".xml", ".rels", ".vml")):
                    try:
                        root = ET.fromstring(data)
                    except ET.ParseError as exc:
                        raise ValueError(f"Could not safely process workbook XML: {name}") from exc

                    def clean_tree(parent: ET.Element) -> None:
                        for child in list(parent):
                            child_name = local_name(child.tag)
                            if child_name == "hyperlink":
                                parent.remove(child)
                                counts["hyperlinks"] += 1
                                continue
                            if child_name == "externalReferences":
                                parent.remove(child)
                                counts["external_links"] += 1
                                continue
                            if child_name == "Relationship":
                                relation_type = child.attrib.get("Type", "")
                                if relation_type.endswith(("/hyperlink", "/externalLinkPath")):
                                    parent.remove(child)
                                    counts["hyperlinks" if relation_type.endswith("/hyperlink") else "external_links"] += 1
                                    continue
                            if child_name == "Override" and child.attrib.get("PartName", "").startswith(
                                "/xl/externalLinks/"
                            ):
                                parent.remove(child)
                                counts["external_links"] += 1
                                continue

                            if child_name == "f" and "HYPERLINK(" in (child.text or "").upper():
                                parent.remove(child)
                                counts["formulas"] += 1
                                continue
                            clean_tree(child)

                    clean_tree(root)

                    for element in root.iter():
                        if element.text:
                            cleaned, count = remove_links(element.text.encode("utf-8"))
                            element.text = cleaned.decode("utf-8")
                            counts["urls"] += count
                        if element.tail:
                            cleaned, count = remove_links(element.tail.encode("utf-8"))
                            element.tail = cleaned.decode("utf-8")
                            counts["urls"] += count
                        for key, value in list(element.attrib.items()):
                            cleaned, count = remove_links(value.encode("utf-8"))
                            element.attrib[key] = cleaned.decode("utf-8")
                            counts["urls"] += count

                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)

                target.writestr(info, data)

        shutil.copystat(path, temp_path)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return counts


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def split_oversized_file(path: Path) -> list[Path]:
    if path.stat().st_size <= MAX_FILE_BYTES:
        return []

    temp_dir = Path(tempfile.mkdtemp(prefix=".split-parts-", dir=path.parent))
    temporary_parts: list[Path] = []
    final_parts: list[Path] = []
    try:
        with path.open("rb") as source:
            index = 0
            while True:
                block = source.read(PART_BYTES)
                if not block:
                    break
                part = temp_dir / f"part-{index:02d}"
                part.write_bytes(block)
                temporary_parts.append(part)
                index += 1

        original_hash = file_hash(path)
        combined = hashlib.sha256()
        for part in temporary_parts:
            with part.open("rb") as source:
                for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    combined.update(block)
        if combined.hexdigest() != original_hash:
            raise RuntimeError(f"Part verification failed for {path}")
        if any(part.stat().st_size > MAX_FILE_BYTES for part in temporary_parts):
            raise RuntimeError(f"A generated part exceeds GitHub's file limit: {path}")

        final_parts = [
            path.with_name(f"{path.name}.part-{index:02d}")
            for index in range(len(temporary_parts))
        ]
        if any(part.exists() for part in final_parts):
            raise FileExistsError(f"Refusing to overwrite existing parts for {path}")

        for source, destination in zip(temporary_parts, final_parts):
            os.replace(source, destination)
        path.unlink()
        return final_parts
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    if not ROOT.is_dir():
        raise FileNotFoundError(f"Export folder not found: {ROOT}")

    files = sorted(path for path in ROOT.rglob("*") if path.is_file())
    unsupported = [
        path for path in files
        if not is_json_file(path) and path.suffix.lower() != ".xlsx"
    ]
    if unsupported:
        names = ", ".join(str(path.relative_to(ROOT)) for path in unsupported[:10])
        raise ValueError(f"Unsupported file type(s); no changes made: {names}")

    backup = BACKUP_PARENT / (
        f"{ROOT.name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copytree(ROOT, backup)
    print(f"Backup created: {backup}")

    totals = {"files": 0, "urls": 0, "hyperlinks": 0, "external_links": 0, "formulas": 0}
    split_count = 0
    part_count = 0
    existing_parts = 0

    for path in files:
        if is_json_part(path):
            # Parts are fragments of one JSON document; the original document
            # was sanitized before splitting, so do not parse them separately.
            existing_parts += 1
            continue
        if is_json_file(path):
            totals["urls"] += sanitize_text_file(path)
        elif path.suffix.lower() == ".xlsx":
            result = sanitize_xlsx(path)
            for key in ("urls", "hyperlinks", "external_links", "formulas"):
                totals[key] += result[key]
        totals["files"] += 1

        parts = split_oversized_file(path)
        if parts:
            split_count += 1
            part_count += len(parts)
            print(
                f"Split {path.relative_to(ROOT)} into {len(parts)} parts: "
                + ", ".join(f"{part.stat().st_size} bytes" for part in parts)
            )

    print(
        "Done. "
        f"Processed {totals['files']} files; removed {totals['urls']} URL/link strings, "
        f"{totals['hyperlinks']} Excel hyperlinks, {totals['formulas']} HYPERLINK formulas, "
        f"and {totals['external_links']} external-link entries. "
        f"Split {split_count} oversized file(s) into {part_count} parts."
    )
    if existing_parts:
        print(f"Skipped {existing_parts} previously generated JSON part(s).")
    print(f"Original export folder updated: {ROOT}")
    print(f"Backup retained at: {backup}")


if __name__ == "__main__":
    main()
