#!/usr/bin/env python3
"""
jxcl.py - Convert between JSON, XML, and CSV with nested support.

Key design choices:
- CSV cannot natively represent nested structures reliably.
  We support two strategies:
  1) "json" (default): store nested dict/list values as JSON strings in a cell.
  2) "flatten": flatten nested dicts into dotted keys; lists are JSON strings unless explode is used.
  3) "explode": if the top-level JSON is a list of objects (or has a list at a path),
     write one CSV row per list element; nested values still handled via flatten/json encoding.

- XML mapping:
  JSON -> XML uses:
    * dict keys => tags
    * list => repeated <item> elements under its parent
    * attributes are not used by default
  XML -> JSON uses:
    * repeated tags become lists
    * leaf text is parsed into int/float/bool/null when possible; otherwise string
    * attributes are stored under "@attr" if present

This tool aims for correctness, safety, and predictable round-tripping where possible.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional, Iterable

from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


# -----------------------------
# Utility: IO helpers
# -----------------------------

def read_text(path: str, encoding: str = "utf-8") -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding=encoding, newline="") as f:
        return f.read()

def write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    if path == "-":
        sys.stdout.write(text)
        return
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)

def die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# -----------------------------
# JSON helpers
# -----------------------------

def load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        die(f"Invalid JSON: {e}")

def dump_json(obj: Any, pretty: bool = True) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


# -----------------------------
# XML <-> JSON mapping
# -----------------------------

def _coerce_scalar(s: str) -> Any:
    t = s.strip()
    if t == "":
        return ""
    low = t.lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    # int?
    try:
        if t.startswith("0") and len(t) > 1 and t[1].isdigit():
            # preserve as string to avoid surprising octal-like cases
            raise ValueError
        return int(t)
    except ValueError:
        pass
    # float?
    try:
        return float(t)
    except ValueError:
        return t

def xml_to_json(xml_text: str) -> Any:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        die(f"Invalid XML: {e}")

    def elem_to_obj(elem: ET.Element) -> Any:
        # Attributes
        attrs = dict(elem.attrib) if elem.attrib else None

        # Children
        children = list(elem)
        if not children:
            text = elem.text or ""
            val = _coerce_scalar(text)
            if attrs:
                return {"@attr": attrs, "#text": val}
            return val

        # Group children by tag
        grouped: Dict[str, List[Any]] = {}
        for c in children:
            grouped.setdefault(c.tag, []).append(elem_to_obj(c))

        obj: Dict[str, Any] = {}
        for tag, items in grouped.items():
            # If tag repeats, list; otherwise single
            if len(items) == 1:
                obj[tag] = items[0]
            else:
                obj[tag] = items

        # If element has meaningful text besides whitespace, keep it
        if (elem.text or "").strip():
            obj["#text"] = _coerce_scalar(elem.text or "")

        if attrs:
            obj["@attr"] = attrs

        return obj

    return {root.tag: elem_to_obj(root)}

def json_to_xml(obj: Any, root_name: str = "root", xml_declaration: bool = True) -> str:
    def build(parent: ET.Element, value: Any, key_hint: str = "item") -> None:
        if value is None:
            # Represent null as empty element with attribute
            parent.set("xsi:nil", "true")
            return

        if isinstance(value, (str, int, float, bool)):
            parent.text = str(value)
            return

        if isinstance(value, list):
            for item in value:
                child = ET.SubElement(parent, "item")
                build(child, item, "item")
            return

        if isinstance(value, dict):
            # Special handling: if it looks like {"@attr": {...}, "#text": ...}
            attrs = value.get("@attr") if isinstance(value.get("@attr"), dict) else None
            if attrs:
                for k, v in attrs.items():
                    parent.set(str(k), str(v))

            # Text node
            if "#text" in value and not any(k for k in value.keys() if k not in ("@attr", "#text")):
                parent.text = "" if value["#text"] is None else str(value["#text"])
                return

            for k, v in value.items():
                if k in ("@attr", "#text"):
                    continue
                tag = str(k)
                child = ET.SubElement(parent, tag)
                build(child, v, tag)
            return

        # Fallback: string
        parent.text = str(value)

    # If the object is already a single-root dict, keep that root unless overridden by root_name
    if isinstance(obj, dict) and len(obj) == 1:
        (only_key, only_val), = obj.items()
        root_tag = root_name or str(only_key)
        root = ET.Element(root_tag)
        build(root, only_val, only_key)
    else:
        root = ET.Element(root_name)
        build(root, obj, root_name)

    # Ensure XML escaping is handled by serializer; ElementTree does this.
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=xml_declaration)
    return xml_bytes.decode("utf-8") + "\n"


# -----------------------------
# CSV <-> JSON (nested-aware)
# -----------------------------

def _json_cell_encode(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if v is None:
        return ""
    return str(v)

def _try_json_cell_decode(s: str) -> Any:
    t = s.strip()
    if t == "":
        return ""
    # attempt JSON object/array/number/bool/null
    if t[0] in "[{\"" or t in ("true", "false", "null") or t[0].isdigit() or t[0] == "-":
        try:
            return json.loads(t)
        except Exception:
            pass
    # basic scalar coercion
    return _coerce_scalar(t)

def flatten_json(
    obj: Any,
    prefix: str = "",
    sep: str = ".",
    keep_lists_as_json: bool = True
) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}

    def rec(v: Any, path: str) -> None:
        if isinstance(v, dict):
            if not v:
                flat[path] = {}
                return
            for k, vv in v.items():
                new_path = f"{path}{sep}{k}" if path else str(k)
                rec(vv, new_path)
            return

        if isinstance(v, list):
            if keep_lists_as_json:
                flat[path] = v
            else:
                # index keys: a[0], a[1]...
                for i, vv in enumerate(v):
                    new_path = f"{path}[{i}]"
                    rec(vv, new_path)
            return

        flat[path] = v

    rec(obj, prefix)
    # if obj is scalar and prefix empty
    if "" in flat and prefix == "":
        return {"value": flat[""]}
    return flat

def unflatten_json(flat: Dict[str, Any], sep: str = ".") -> Any:
    # Supports dotted keys and [index] keys if present.
    root: Any = {}

    def set_path(d: Any, path: str, value: Any) -> Any:
        tokens: List[Any] = []
        i = 0
        buf = ""
        while i < len(path):
            ch = path[i]
            if ch == sep:
                if buf:
                    tokens.append(buf)
                    buf = ""
                i += 1
                continue
            if ch == "[":
                if buf:
                    tokens.append(buf)
                    buf = ""
                j = path.find("]", i)
                if j == -1:
                    tokens.append(path[i:])
                    break
                idx = path[i+1:j]
                try:
                    tokens.append(int(idx))
                except ValueError:
                    tokens.append(idx)
                i = j + 1
                continue
            buf += ch
            i += 1
        if buf:
            tokens.append(buf)

        cur = d
        for t in tokens[:-1]:
            nxt = tokens[tokens.index(t)+1] if tokens.index(t) < len(tokens)-1 else None  # safe-ish
            if isinstance(t, int):
                if not isinstance(cur, list):
                    cur_list: List[Any] = []
                    # attach to parent by returning, handled outside
                    cur = cur_list
                while len(cur) <= t:
                    cur.append({})
                if cur[t] == {} and isinstance(nxt, int):
                    cur[t] = []
                cur = cur[t]
            else:
                if not isinstance(cur, dict):
                    cur = {}
                if t not in cur or cur[t] is None:
                    cur[t] = [] if isinstance(nxt, int) else {}
                cur = cur[t]

        last = tokens[-1] if tokens else "value"
        if isinstance(last, int):
            if not isinstance(cur, list):
                # convert
                cur_list = []
                # cannot reattach easily; but this case is rare in our usage
                cur = cur_list
            while len(cur) <= last:
                cur.append(None)
            cur[last] = value
        else:
            if not isinstance(cur, dict):
                cur = {}
            cur[last] = value
        return d

    for k, v in flat.items():
        set_path(root, k, v)
    # If it was a single "value" wrapper, unwrap
    if isinstance(root, dict) and set(root.keys()) == {"value"}:
        return root["value"]
    return root

def json_to_csv(
    obj: Any,
    mode: str = "json",  # json|flatten|explode
    explode_path: Optional[str] = None,
    sep: str = "."
) -> str:
    # Determine rows (list of dicts)
    rows: List[Dict[str, Any]] = []

    def get_by_path(o: Any, path: str) -> Any:
        cur = o
        for part in path.split(sep) if path else []:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    if mode == "explode":
        target = None
        if explode_path:
            target = get_by_path(obj, explode_path)
        else:
            target = obj

        if not isinstance(target, list):
            die("explode mode requires the input (or explode_path) to point to a JSON array")

        for item in target:
            if not isinstance(item, dict):
                # allow scalar items, wrap
                item = {"value": item}
            flat = flatten_json(item, sep=sep, keep_lists_as_json=True)
            rows.append(flat)
    else:
        if isinstance(obj, list):
            # list of rows
            for item in obj:
                if mode == "flatten":
                    rows.append(flatten_json(item, sep=sep, keep_lists_as_json=True))
                else:
                    # json mode expects flat dict; nested stored as JSON strings by leaving as-is
                    if isinstance(item, dict):
                        rows.append(item)
                    else:
                        rows.append({"value": item})
        elif isinstance(obj, dict):
            if mode == "flatten":
                rows.append(flatten_json(obj, sep=sep, keep_lists_as_json=True))
            else:
                rows.append(obj)
        else:
            rows.append({"value": obj})

    # Collect header fields
    fieldnames: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()

    for r in rows:
        row_out: Dict[str, str] = {}
        for k in fieldnames:
            v = r.get(k, "")
            if mode in ("flatten", "explode"):
                row_out[k] = _json_cell_encode(v)
            else:
                # json mode: nested dict/list still needs cell encoding
                row_out[k] = _json_cell_encode(v)
        writer.writerow(row_out)

    return out.getvalue()

def csv_to_json(
    csv_text: str,
    mode: str = "json",  # json|flatten
    sep: str = "."
) -> Any:
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    rows: List[Dict[str, Any]] = []
    for row in reader:
        parsed: Dict[str, Any] = {}
        for k, v in row.items():
            parsed[k] = _try_json_cell_decode(v or "")
        rows.append(parsed)

    if mode == "flatten":
        # unflatten each row
        return [unflatten_json(r, sep=sep) for r in rows]
    return rows


# -----------------------------
# JSON Schema validation (optional)
# -----------------------------

def validate_json_schema(instance: Any, schema_text: str) -> None:
    try:
        import jsonschema
        from jsonschema import Draft202012Validator
    except Exception:
        die("JSON Schema validation requires 'jsonschema'. Install with: pip install jsonschema")

    schema = load_json(schema_text)

    # Best-effort validator selection
    try:
        validator = Draft202012Validator(schema)
    except Exception:
        validator = jsonschema.Draft7Validator(schema)

    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if not errors:
        return

    def format_path(e: Any) -> str:
        if not e.path:
            return "$"
        parts = ["$"]
        for p in e.path:
            if isinstance(p, int):
                parts.append(f"[{p}]")
            else:
                # escape dot-ish keys
                if any(ch in str(p) for ch in ".[]"):
                    parts.append(f"['{p}']")
                else:
                    parts.append(f".{p}")
        return "".join(parts)

    lines = []
    for e in errors:
        path = format_path(e)
        # Provide a friendlier message for common validators
        msg = e.message

        # Enrich required-property failures with which are missing (jsonschema already does, but we add path)
        lines.append(f"- {path}: {msg}")

        # Show one level of context if useful
        if e.context:
            for sub in e.context[:3]:
                lines.append(f"    - {format_path(sub)}: {sub.message}")

    die("JSON Schema validation failed:\n" + "\n".join(lines), code=3)


# -----------------------------
# CLI
# -----------------------------

def detect_format(fmt: str, in_path: str) -> str:
    if fmt != "auto":
        return fmt
    if in_path == "-":
        die("input format is 'auto' but input is stdin; please specify --in json|xml|csv")
    ext = os.path.splitext(in_path.lower())[1]
    if ext in (".json",):
        return "json"
    if ext in (".xml",):
        return "xml"
    if ext in (".csv",):
        return "csv"
    die("could not auto-detect input format from extension; please specify --in json|xml|csv")

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert between JSON, XML, and CSV with nested support.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--in", dest="in_fmt", default="auto", choices=["auto", "json", "xml", "csv"], help="Input format")
    p.add_argument("--out", dest="out_fmt", required=True, choices=["json", "xml", "csv"], help="Output format")
    p.add_argument("-i", "--input", default="-", help="Input file path or '-' for stdin")
    p.add_argument("-o", "--output", default="-", help="Output file path or '-' for stdout")
    p.add_argument("--encoding", default="utf-8", help="File encoding")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    p.add_argument("--root", default="root", help="XML root element name when producing XML")

    # CSV options
    p.add_argument("--csv-mode", default="json", choices=["json", "flatten", "explode"],
                   help="How to represent nested data when writing CSV / interpreting CSV")
    p.add_argument("--explode-path", default=None,
                   help="In explode mode, dot-path to the array to explode into rows (e.g., 'data.items')")
    p.add_argument("--sep", default=".", help="Path separator used for flatten/unflatten keys")

    # Schema validation
    p.add_argument("--schema", default=None, help="Path to JSON Schema file; validates the JSON just before output")

    args = p.parse_args(argv)

    in_fmt = detect_format(args.in_fmt, args.input)
    raw_in = read_text(args.input, encoding=args.encoding)

    # Parse input into JSON-like Python object (our intermediate)
    if in_fmt == "json":
        data = load_json(raw_in)
    elif in_fmt == "xml":
        data = xml_to_json(raw_in)
    elif in_fmt == "csv":
        # For CSV, choose mode:
        # - json: rows -> list[dict], values decoded from JSON if possible
        # - flatten: rows -> list[unflattened dict]
        data = csv_to_json(raw_in, mode=("flatten" if args.csv_mode in ("flatten",) else "json"), sep=args.sep)
    else:
        die(f"unsupported input format: {in_fmt}")

    # Validate schema (on the JSON intermediate)
    if args.schema:
        schema_text = read_text(args.schema, encoding=args.encoding)
        validate_json_schema(data, schema_text)

    # Convert from intermediate to output
    if args.out_fmt == "json":
        out = dump_json(data, pretty=args.pretty)
    elif args.out_fmt == "xml":
        out = json_to_xml(data, root_name=args.root, xml_declaration=True)
    elif args.out_fmt == "csv":
        out = json_to_csv(data, mode=args.csv_mode, explode_path=args.explode_path, sep=args.sep)
    else:
        die(f"unsupported output format: {args.out_fmt}")

    write_text(args.output, out, encoding=args.encoding)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())