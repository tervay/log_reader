import argparse
import fnmatch
import functools
import hashlib
import json
import math
import os
import platform
import re
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import orjson
from wpiutil.log import DataLogReader

OWLET_INDEX = "https://redist.ctr-electronics.com/index.json"
SCHEMA_PREFIX = "/.schema/struct:"
GETTERS = {
    "boolean": "getBoolean",
    "int64": "getInteger",
    "float": "getFloat",
    "double": "getDouble",
    "string": "getString",
    "boolean[]": "getBooleanArray",
    "int64[]": "getIntegerArray",
    "float[]": "getFloatArray",
    "double[]": "getDoubleArray",
    "string[]": "getStringArray",
}
PRIMS = {
    "bool": "?",
    "char": "c",
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float": "f",
    "float32": "f",
    "double": "d",
    "float64": "d",
}
DECL = re.compile(
    r"(?:(?:enum)?\s*\{(?P<enum>[^}]*)\})?\s*(?P<type>\w+)\s+(?P<name>\w+)\s*(?:\[\s*(?P<count>\d+)\s*\]|:\s*(?P<bits>\d+))?"
)


def parse_schema(text):
    fields = []
    for decl in text.split(";"):
        if m := DECL.fullmatch(decl.strip()):
            pairs = [p.split("=") for p in (m["enum"] or "").split(",") if p.strip()]
            enum = {int(v): k.strip() for k, v in pairs}
            count = int(m["count"]) if m["count"] else None
            fields.append((m["type"], m["name"], count, int(m["bits"] or 0), enum))
    return fields


def decode_struct(schemas, name, data, pos=0):
    out, bucket = {}, None
    for ftype, fname, count, bits, enum in schemas[name]:
        named = lambda v: enum.get(v, v)
        if bits:
            size = struct.calcsize(PRIMS[ftype])
            if not bucket or bucket[2] + bits > bucket[0] * 8 or (ftype != "bool" and bucket[0] != size):
                bucket = [size, int.from_bytes(data[pos : pos + size], "little"), 0]
                pos += size
            value = bucket[1] >> bucket[2] & (1 << bits) - 1
            bucket[2] += bits
            if PRIMS[ftype].islower() and value >> bits - 1:
                value -= 1 << bits
            out[fname] = bool(value) if ftype == "bool" else named(value)
            continue
        bucket = None
        if ftype == "char":
            out[fname] = data[pos : pos + (count or 1)].rstrip(b"\0").decode("utf-8", "replace")
            pos += count or 1
        elif ftype in PRIMS:
            fmt = f"<{count or 1}{PRIMS[ftype]}"
            values = [named(v) for v in struct.unpack_from(fmt, data, pos)]
            pos += struct.calcsize(fmt)
            out[fname] = values if count else values[0]
        else:
            values = []
            for _ in range(count or 1):
                value, pos = decode_struct(schemas, ftype, data, pos)
                values.append(value)
            out[fname] = values if count else values[0]
    return out, pos


def decode(record, type, schemas):
    try:
        if type in GETTERS:
            return getattr(record, GETTERS[type])()
        if type == "json":
            return json.loads(record.getString())
        if type.startswith("struct:"):
            name, data = type[7:].removesuffix("[]"), record.getRaw()
            if not type.endswith("[]"):
                return decode_struct(schemas, name, data)[0]
            values, pos = [], 0
            while schemas[name] and pos < len(data):
                value, pos = decode_struct(schemas, name, data, pos)
                values.append(value)
            return values
    except (KeyError, ValueError, TypeError, struct.error):
        pass
    return record.getRaw().hex()


def emit(obj):
    sys.stdout.buffer.write(orjson.dumps(obj) + b"\n")


def open_log(path):
    reader = DataLogReader(str(path))
    if not reader.isValid():
        sys.exit(f"error: {path} is not a valid wpilog file")
    return reader


def records(reader, schema=False):
    entries = {}
    for r in reader:
        if r.isStart():
            d = r.getStartData()
            entries[d.entry] = {"name": d.name, "type": d.type, "metadata": d.metadata}
        elif r.isSetMetadata():
            d = r.getSetMetadataData()
            if d.entry in entries:
                entries[d.entry]["metadata"] = d.metadata
        elif r.isFinish():
            entries.pop(r.getFinishEntry(), None)
        elif not r.isControl() and (e := entries.get(r.getEntry())) and e["name"].startswith("/.schema/") == schema:
            yield r, e


def struct_schemas(reader):
    return {
        e["name"][len(SCHEMA_PREFIX) :]: parse_schema(r.getString())
        for r, e in records(reader, schema=True)
        if e["name"].startswith(SCHEMA_PREFIX)
    }


def info(path, name):
    reader, stats = open_log(path), {}
    for r, e in records(reader):
        t = r.getTimestamp() / 1e6
        s = stats.setdefault(e["name"], {**e, "count": 0, "first": t, "last": t})
        s["count"] += 1
        s["first"], s["last"] = min(s["first"], t), max(s["last"], t)
    entries = sorted(stats.values(), key=lambda s: s["name"])
    emit(
        {
            "file": str(name),
            "extraHeader": reader.getExtraHeader(),
            "start": min((s["first"] for s in entries), default=None),
            "end": max((s["last"] for s in entries), default=None),
            "entries": entries,
        }
    )


def read(path, patterns, start, end, limit):
    reader, count = open_log(path), 0
    schemas = struct_schemas(reader)
    wanted = functools.cache(lambda name: not patterns or any(fnmatch.fnmatchcase(name, p) for p in patterns))
    for r, e in records(reader):
        t = r.getTimestamp() / 1e6
        if not wanted(e["name"]) or not start <= t <= end:
            continue
        emit({"t": t, "name": e["name"], "value": decode(r, e["type"], schemas)})
        count += 1
        if count == limit:
            return


def owlet_platform():
    if platform.system() == "Darwin":
        return "macosuniversal"
    if platform.system() == "Windows":
        return "windowsx86-64"
    return "linuxarm64" if platform.machine().lower() in ("aarch64", "arm64") else "linuxx86-64"


def pick_owlet(index, compliancy):
    owlet = next(t for t in index["Tools"] if t["Name"] == "owlet")
    items = [i for i in owlet["Items"] if i["Compliancy"] == compliancy]
    if not items:
        sys.exit(f"error: no owlet release supports hoot compliancy {compliancy}")
    return max(items, key=lambda i: [int(n) for n in re.findall(r"\d+", i["Version"])])


def owlet_path(compliancy):
    folder = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    suffix = ".exe" if os.name == "nt" else ""
    if cached := sorted(folder.glob(f"owlet-*-C{compliancy}{suffix}")):
        return cached[-1]
    with urllib.request.urlopen(OWLET_INDEX) as f:
        item = pick_owlet(json.load(f), compliancy)
    key = owlet_platform()
    print(f"downloading owlet {item['Version']} to {folder}", file=sys.stderr)
    with urllib.request.urlopen(item["Urls"][key]) as f:
        data = f.read()
    if hashlib.sha1(data).hexdigest() != item["Urls"][f"{key}-sha1"]:
        sys.exit("error: owlet download failed its checksum")
    path = folder / f"owlet-{item['Version']}-C{compliancy}{suffix}"
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def hoot_to_wpilog(hoot, tmp):
    with open(hoot, "rb") as f:
        f.seek(70)
        compliancy = f.read(1)[0]
    if compliancy < 6:
        sys.exit("error: hoot logs from before Phoenix 2024 are not supported")
    out = Path(tmp) / "out.wpilog"
    subprocess.run([owlet_path(compliancy), hoot, out, "-f", "wpilog"], check=True, stdout=sys.stderr)
    return out


def main():
    parser = argparse.ArgumentParser(prog="wpilog", description="Read WPILib .wpilog or CTRE .hoot files as JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    info_cmd = sub.add_parser("info", help="list entries with type, metadata, count and time range")
    info_cmd.add_argument("file", type=Path)
    read_cmd = sub.add_parser("read", help="print records as JSON Lines: {t, name, value}")
    read_cmd.add_argument("file", type=Path)
    read_cmd.add_argument("-e", "--entry", action="append", default=[], help="entry name glob, repeatable")
    read_cmd.add_argument("--start", type=float, default=-math.inf, help="first timestamp in seconds")
    read_cmd.add_argument("--end", type=float, default=math.inf, help="last timestamp in seconds")
    read_cmd.add_argument("--limit", type=int, default=0, help="stop after N records")
    args = parser.parse_args()
    if not args.file.is_file():
        sys.exit(f"error: {args.file} not found")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = hoot_to_wpilog(args.file, tmp) if args.file.suffix.lower() == ".hoot" else args.file
            if args.command == "info":
                info(path, args.file)
            else:
                read(path, args.entry, args.start, args.end, args.limit)
            sys.stdout.flush()
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: owlet failed with exit code {e.returncode}")
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


if __name__ == "__main__":
    main()
