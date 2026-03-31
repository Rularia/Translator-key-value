from __future__ import annotations

from copy import deepcopy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, unescape


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
MODE_AUTO = "auto"
MODE_JSON_KEY = "json_key_to_value"
MODE_JSON_VALUE = "json_value_to_value"
MODE_EQUALS = "equals_right_value"
MODE_XML = "xml_string_value"
MODE_LABELS = {
    MODE_AUTO: "Auto Detect",
    MODE_JSON_KEY: "Mode A: Key -> Value",
    MODE_JSON_VALUE: "Mode B: Value -> Value",
    MODE_EQUALS: "Mode C: Left=Right",
    MODE_XML: "Mode D: XML String Value",
}
_TEXT_EXTENSIONS = {".txt", ".ini", ".cfg", ".lang", ".properties"}
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "gb18030")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
_NUMBER_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")
_XML_STRING_PATTERN = re.compile(
    r'(<string\b(?P<attrs>[^>]*)\bid=(?P<quote>["\"])(?P<id>.*?)(?P=quote)(?P<rest>[^>]*)>)(?P<body>.*?)(</string>)',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(slots=True)
class TranslationEntry:
    pointer: str
    key_text: str
    source_text: str
    translated_text: str
    kind: str


def available_source_modes() -> list[tuple[str, str]]:
    return [(mode, MODE_LABELS[mode]) for mode in (MODE_AUTO, MODE_JSON_KEY, MODE_JSON_VALUE, MODE_EQUALS, MODE_XML)]


def normalize_source_mode(mode: str | None, *, allow_auto: bool = True) -> str:
    value = str(mode or MODE_AUTO).strip() or MODE_AUTO
    if value == MODE_AUTO and allow_auto:
        return MODE_AUTO
    if value in (MODE_JSON_KEY, MODE_JSON_VALUE, MODE_EQUALS, MODE_XML):
        return value
    return MODE_JSON_VALUE


def source_mode_label(mode: str | None) -> str:
    normalized = normalize_source_mode(mode)
    return MODE_LABELS.get(normalized, MODE_LABELS[MODE_JSON_VALUE])


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def classify_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return "blank"
    if _NUMBER_PATTERN.fullmatch(stripped):
        return "number"
    if _ID_PATTERN.fullmatch(stripped):
        return "identifier"
    return "text"


def _read_text_with_encoding(path: str | Path) -> tuple[str, str]:
    path_obj = Path(path)
    last_error: UnicodeDecodeError | None = None
    for encoding in _TEXT_ENCODINGS:
        try:
            return path_obj.read_text(encoding=encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return path_obj.read_text(encoding="utf-8"), "utf-8"


def _read_text_with_fallbacks(path: str | Path) -> str:
    text, _encoding = _read_text_with_encoding(path)
    return text


def load_json_file(path: str | Path) -> JsonValue:
    return json.loads(_read_text_with_fallbacks(path))


def detect_source_encoding(path: str | Path) -> str:
    _text, encoding = _read_text_with_encoding(path)
    return encoding


def save_json_file(path: str | Path, data: JsonValue, *, encoding: str = "utf-8") -> None:
    with Path(path).open("w", encoding=encoding, newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_source_file(path: str | Path, mode: str = MODE_AUTO) -> tuple[Any, str, str]:
    path_obj = Path(path)
    normalized = normalize_source_mode(mode)
    if normalized == MODE_XML or (normalized == MODE_AUTO and path_obj.suffix.casefold() == ".xml"):
        data = _load_xml_file(path_obj)
        return data, MODE_XML, str(data.get("_encoding", "utf-8"))
    if normalized == MODE_EQUALS or (normalized == MODE_AUTO and _looks_like_equals_file(path_obj)):
        data = _load_equals_file(path_obj)
        return data, MODE_EQUALS, str(data.get("_encoding", "utf-8"))

    encoding = detect_source_encoding(path_obj)
    data = load_json_file(path_obj)
    detected_mode = _detect_json_mode(data) if normalized == MODE_AUTO else normalize_source_mode(normalized, allow_auto=False)
    return data, detected_mode, encoding


def save_source_file(path: str | Path, data: Any, mode: str, *, encoding: str = "utf-8") -> None:
    normalized = normalize_source_mode(mode, allow_auto=False)
    if normalized == MODE_EQUALS:
        _save_equals_file(path, data, encoding=encoding)
        return
    if normalized == MODE_XML:
        _save_xml_file(path, data, encoding=encoding)
        return
    save_json_file(path, data, encoding=encoding)


def extract_translation_entries(data: Any, mode: str = MODE_JSON_VALUE) -> list[TranslationEntry]:
    normalized = normalize_source_mode(mode, allow_auto=False)
    if normalized == MODE_JSON_KEY:
        return _extract_json_key_entries(data)
    if normalized == MODE_EQUALS:
        return _extract_equals_entries(data)
    if normalized == MODE_XML:
        return _extract_xml_entries(data)
    return _extract_json_value_entries(data)


def apply_translations(
    data: Any,
    entries: list[TranslationEntry],
    *,
    mode: str = MODE_JSON_VALUE,
    skip_blank: bool = True,
) -> Any:
    normalized = normalize_source_mode(mode, allow_auto=False)
    if normalized == MODE_XML:
        return _apply_xml_translations(data, entries, skip_blank=skip_blank)

    updated = deepcopy(data)
    for entry in entries:
        if skip_blank and not entry.translated_text.strip():
            continue
        _set_pointer_value(updated, entry.pointer, entry.translated_text)
    return updated


def _detect_json_mode(data: JsonValue) -> str:
    stats = {"blank_values": 0, "text_keys": 0, "text_values": 0, "string_leaves": 0}

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    stats["string_leaves"] += 1
                    if not value.strip():
                        stats["blank_values"] += 1
                    if classify_text(str(key)) == "text":
                        stats["text_keys"] += 1
                    if classify_text(value) == "text":
                        stats["text_values"] += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(data)
    if not stats["string_leaves"]:
        return MODE_JSON_VALUE
    if stats["blank_values"] * 2 >= stats["string_leaves"] and stats["text_keys"] >= max(1, stats["text_values"]):
        return MODE_JSON_KEY
    return MODE_JSON_VALUE


def _extract_json_key_entries(data: JsonValue) -> list[TranslationEntry]:
    entries: list[TranslationEntry] = []

    def walk(node: JsonValue, pointer: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_pointer = f"{pointer}/{_escape_pointer_token(str(key))}"
                if isinstance(value, str):
                    source_text = str(key)
                    entries.append(
                        TranslationEntry(
                            pointer=child_pointer,
                            key_text=str(key),
                            source_text=source_text,
                            translated_text=value,
                            kind=classify_text(source_text),
                        )
                    )
                else:
                    walk(value, child_pointer)
            return

        if isinstance(node, list):
            for index, value in enumerate(node):
                child_pointer = f"{pointer}/{index}"
                if isinstance(value, str):
                    entries.append(
                        TranslationEntry(
                            pointer=child_pointer,
                            key_text=f"[{index}]",
                            source_text=value,
                            translated_text=value,
                            kind=classify_text(value),
                        )
                    )
                else:
                    walk(value, child_pointer)

    walk(data)
    return entries


def _extract_json_value_entries(data: JsonValue) -> list[TranslationEntry]:
    entries: list[TranslationEntry] = []

    def walk(node: JsonValue, pointer: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_pointer = f"{pointer}/{_escape_pointer_token(str(key))}"
                if isinstance(value, str):
                    entries.append(
                        TranslationEntry(
                            pointer=child_pointer,
                            key_text=str(key),
                            source_text=value,
                            translated_text=value,
                            kind=classify_text(value),
                        )
                    )
                else:
                    walk(value, child_pointer)
            return

        if isinstance(node, list):
            for index, value in enumerate(node):
                child_pointer = f"{pointer}/{index}"
                if isinstance(value, str):
                    entries.append(
                        TranslationEntry(
                            pointer=child_pointer,
                            key_text=f"[{index}]",
                            source_text=value,
                            translated_text=value,
                            kind=classify_text(value),
                        )
                    )
                else:
                    walk(value, child_pointer)

    walk(data)
    return entries


def _looks_like_equals_file(path: Path) -> bool:
    if path.suffix.casefold() in _TEXT_EXTENSIONS:
        return True
    try:
        text = _read_text_with_fallbacks(path)
    except Exception:
        return False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";", "//")):
            continue
        return "=" in raw_line
    return False


def _split_equals_line(raw_line: str) -> tuple[str, str, str] | None:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith(("#", ";", "//")) or "=" not in raw_line:
        return None
    left, right = raw_line.split("=", 1)
    leading = left.rstrip()
    spacing = left[len(leading):] + "="
    right_leading = right[: len(right) - len(right.lstrip())]
    separator = spacing + right_leading
    return leading, separator, right.lstrip()


def _load_equals_file(path: str | Path) -> dict[str, Any]:
    text, encoding = _read_text_with_encoding(path)
    lines: list[dict[str, Any]] = []
    for index, raw_line in enumerate(text.splitlines()):
        split = _split_equals_line(raw_line)
        if split is None:
            lines.append({"kind": "raw", "raw": raw_line})
            continue
        left, separator, right = split
        lines.append(
            {
                "kind": "pair",
                "left": left,
                "separator": separator,
                "right": right,
                "line_index": index,
            }
        )
    return {"_format": MODE_EQUALS, "_encoding": encoding, "trailing_newline": text.endswith("\n"), "lines": lines}


def _save_equals_file(path: str | Path, data: Any, *, encoding: str = "utf-8") -> None:
    if not isinstance(data, dict) or data.get("_format") != MODE_EQUALS:
        raise ValueError("Unsupported equals-line source data.")
    rendered: list[str] = []
    for line in data.get("lines", []):
        if line.get("kind") == "pair":
            rendered.append(f"{line.get('left', '')}{line.get('separator', '=')}{line.get('right', '')}")
        else:
            rendered.append(str(line.get("raw", "")))
    text = "\n".join(rendered)
    if data.get("trailing_newline"):
        text += "\n"
    Path(path).write_text(text, encoding=encoding, newline="\n")


def _extract_equals_entries(data: Any) -> list[TranslationEntry]:
    if not isinstance(data, dict) or data.get("_format") != MODE_EQUALS:
        raise ValueError("Unsupported equals-line source data.")
    entries: list[TranslationEntry] = []
    for index, line in enumerate(data.get("lines", [])):
        if line.get("kind") != "pair":
            continue
        right = str(line.get("right", ""))
        key_text = str(line.get("left", "")).strip() or f"[line {index + 1}]"
        entries.append(
            TranslationEntry(
                pointer=f"/lines/{index}/right",
                key_text=key_text,
                source_text=right,
                translated_text=right,
                kind=classify_text(right),
            )
        )
    return entries


def _load_xml_file(path: str | Path) -> dict[str, Any]:
    text, encoding = _read_text_with_encoding(path)
    return {"_format": MODE_XML, "_encoding": encoding, "text": text}


def _save_xml_file(path: str | Path, data: Any, *, encoding: str = "utf-8") -> None:
    if not isinstance(data, dict) or data.get("_format") != MODE_XML:
        raise ValueError("Unsupported XML source data.")
    Path(path).write_text(str(data.get("text", "")), encoding=encoding, newline="\n")


def _decode_xml_body(body: str) -> str:
    stripped = body.strip()
    if stripped.startswith("<![CDATA[") and stripped.endswith("]]>"):
        return stripped[9:-3]
    return unescape(body)


def _render_xml_body(original_body: str, translated_text: str) -> str:
    stripped = original_body.strip()
    leading = original_body[: len(original_body) - len(original_body.lstrip())]
    trailing = original_body[len(original_body.rstrip()) :]
    if stripped.startswith("<![CDATA[") and stripped.endswith("]]>"):
        return f"{leading}<![CDATA[{translated_text}]]>{trailing}"
    return f"{leading}{escape(translated_text)}{trailing}"


def _extract_xml_entries(data: Any) -> list[TranslationEntry]:
    if not isinstance(data, dict) or data.get("_format") != MODE_XML:
        raise ValueError("Unsupported XML source data.")
    entries: list[TranslationEntry] = []
    text = str(data.get("text", ""))
    for index, match in enumerate(_XML_STRING_PATTERN.finditer(text)):
        source_text = _decode_xml_body(match.group("body"))
        key_text = match.group("id")
        entries.append(
            TranslationEntry(
                pointer=f"/strings/{index}",
                key_text=key_text,
                source_text=source_text,
                translated_text=source_text,
                kind=classify_text(source_text),
            )
        )
    return entries


def _apply_xml_translations(data: Any, entries: list[TranslationEntry], *, skip_blank: bool = True) -> Any:
    if not isinstance(data, dict) or data.get("_format") != MODE_XML:
        raise ValueError("Unsupported XML source data.")
    original_text = str(data.get("text", ""))
    entries_by_pointer = {entry.pointer: entry for entry in entries}
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(_XML_STRING_PATTERN.finditer(original_text)):
        pointer = f"/strings/{index}"
        entry = entries_by_pointer.get(pointer)
        parts.append(original_text[cursor:match.start("body")])
        if entry is None or (skip_blank and not entry.translated_text.strip()):
            parts.append(match.group("body"))
        else:
            parts.append(_render_xml_body(match.group("body"), entry.translated_text))
        cursor = match.end("body")
    parts.append(original_text[cursor:])
    return {"_format": MODE_XML, "text": "".join(parts)}


def _set_pointer_value(root: Any, pointer: str, value: str) -> None:
    if not pointer.startswith("/"):
        raise ValueError(f"Unsupported pointer: {pointer}")

    tokens = [_unescape_pointer_token(token) for token in pointer.lstrip("/").split("/")]
    parent: Any = root
    for token in tokens[:-1]:
        if isinstance(parent, list):
            parent = parent[int(token)]
        else:
            parent = parent[token]

    last = tokens[-1]
    if isinstance(parent, list):
        parent[int(last)] = value
    else:
        parent[last] = value


def entries_to_dict_rows(entries: list[TranslationEntry]) -> list[dict[str, str]]:
    return [
        {
            "pointer": entry.pointer,
            "key": entry.key_text,
            "kind": entry.kind,
            "source": entry.source_text,
            "translation": entry.translated_text,
        }
        for entry in entries
    ]


def rows_to_entries(rows: list[dict[str, Any]]) -> list[TranslationEntry]:
    return [
        TranslationEntry(
            pointer=str(row["pointer"]),
            key_text=str(row["key"]),
            source_text=str(row["source"]),
            translated_text=str(row.get("translation", "")),
            kind=str(row.get("kind", classify_text(str(row["source"])))),
        )
        for row in rows
    ]
