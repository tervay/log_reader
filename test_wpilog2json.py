import json
import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import wpiutil

import wpilog2json

SCRIPT = Path(__file__).with_name("wpilog2json.py")
INNER = "double x;double y"
OUTER = "Inner pos; enum {OFF=0, ON=1} int8 mode; uint8 a:3; uint8 b:5; bool c:1; int16 s:4; char tag[4]; float vals[2]"
OUTER_BYTES = struct.pack("<ddbBBh4s2f", 1.5, -2.0, 1, 5 | 17 << 3, 1, 13, b"ab", 0.5, 2.5)
OUTER_VALUE = {"pos": {"x": 1.5, "y": -2.0}, "mode": "ON", "a": 5, "b": 17, "c": True, "s": -3, "tag": "ab", "vals": [0.5, 2.5]}


@pytest.fixture(scope="module")
def log_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("logs") / "test.wpilog"
    log = wpiutil.DataLogWriter(str(path))
    ids = {name: log.start(name, type, f'{{"src":"{name}"}}', 1) for name, type in [
        ("/.schema/struct:Inner", "structschema"),
        ("/.schema/struct:Outer", "structschema"),
        ("/bool", "boolean"),
        ("/int", "int64"),
        ("/float", "float"),
        ("/double", "double"),
        ("/string", "string"),
        ("/bools", "boolean[]"),
        ("/ints", "int64[]"),
        ("/floats", "float[]"),
        ("/doubles", "double[]"),
        ("/strings", "string[]"),
        ("/json", "json"),
        ("/outer", "struct:Outer"),
        ("/inners", "struct:Inner[]"),
        ("/early", "struct:Inner"),
        ("/missing", "struct:Missing"),
        ("/raw", "raw"),
    ]}
    log.appendRaw(ids["/early"], struct.pack("<2d", 7, 8), 1_000_000)
    log.appendRaw(ids["/.schema/struct:Inner"], INNER.encode(), 2)
    log.appendRaw(ids["/.schema/struct:Outer"], OUTER.encode(), 2)
    for t in (1_000_000, 2_000_000, 3_000_000):
        log.appendDouble(ids["/double"], t / 1e6, t)
    log.appendDouble(ids["/double"], math.nan, 4_000_000)
    log.appendDouble(ids["/double"], math.inf, 5_000_000)
    log.appendBoolean(ids["/bool"], True, 1_000_000)
    log.appendInteger(ids["/int"], -42, 1_000_000)
    log.appendFloat(ids["/float"], 0.25, 1_000_000)
    log.appendString(ids["/string"], "héllo", 1_000_000)
    log.appendBooleanArray(ids["/bools"], [True, False], 1_000_000)
    log.appendIntegerArray(ids["/ints"], [1, -2], 1_000_000)
    log.appendFloatArray(ids["/floats"], [0.5, math.nan], 1_000_000)
    log.appendDoubleArray(ids["/doubles"], [1.5, 2.5], 1_000_000)
    log.appendStringArray(ids["/strings"], ["a", "b"], 1_000_000)
    log.appendString(ids["/json"], '{"k": [1, 2]}', 1_000_000)
    log.appendRaw(ids["/outer"], OUTER_BYTES, 1_000_000)
    log.appendRaw(ids["/inners"], struct.pack("<4d", 1, 2, 3, 4), 1_000_000)
    log.appendRaw(ids["/missing"], b"\x01\x02", 1_000_000)
    log.appendRaw(ids["/raw"], b"\xde\xad", 1_000_000)
    log.flush()
    del log
    return path


def run(*args):
    result = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, encoding="utf-8", check=True)
    return result.stdout


def read(log_file, *args):
    return [json.loads(line, parse_constant=pytest.fail) for line in run("read", log_file, *args).splitlines()]


def values(log_file, name):
    return [r["value"] for r in read(log_file, "-e", name)]


def test_info(log_file):
    info = json.loads(run("info", log_file), parse_constant=pytest.fail)
    by_name = {e["name"]: e for e in info["entries"]}
    assert not any(n.startswith("/.schema") for n in by_name)
    assert by_name["/double"] == {"name": "/double", "type": "double", "metadata": '{"src":"/double"}', "count": 5, "first": 1.0, "last": 5.0}
    assert (info["start"], info["end"]) == (1.0, 5.0)


@pytest.mark.parametrize("name, expected", [
    ("/bool", [True]),
    ("/int", [-42]),
    ("/float", [0.25]),
    ("/double", [1.0, 2.0, 3.0, None, None]),
    ("/string", ["héllo"]),
    ("/bools", [[True, False]]),
    ("/ints", [[1, -2]]),
    ("/floats", [[0.5, None]]),
    ("/doubles", [[1.5, 2.5]]),
    ("/strings", [["a", "b"]]),
    ("/json", [{"k": [1, 2]}]),
    ("/outer", [OUTER_VALUE]),
    ("/inners", [[{"x": 1, "y": 2}, {"x": 3, "y": 4}]]),
    ("/early", [{"x": 7, "y": 8}]),
    ("/missing", ["0102"]),
    ("/raw", ["dead"]),
])
def test_values(log_file, name, expected):
    assert values(log_file, name) == expected


def test_filters(log_file):
    assert [r["t"] for r in read(log_file, "-e", "/doub*", "--start", 2, "--end", 4)] == [2.0, 3.0, 4.0]
    assert {r["name"] for r in read(log_file, "-e", "/bool*", "-e", "/int")} == {"/bool", "/bools", "/int"}
    assert len(read(log_file, "--limit", 3)) == 3


@pytest.mark.skipif(not shutil.which("jq"), reason="jq not installed")
def test_jq(log_file):
    for command in (f"{sys.executable} {SCRIPT} info {log_file} | jq -e .entries[0]", f"{sys.executable} {SCRIPT} read {log_file} | jq -c .value"):
        subprocess.run(command, shell=True, check=True, capture_output=True)


def test_pick_owlet():
    index = {"Tools": [{"Name": "owlet", "Items": [
        {"Version": "26.3.0", "Compliancy": 19},
        {"Version": "26.10.0", "Compliancy": 19},
        {"Version": "26.70.0", "Compliancy": 21},
    ]}]}
    assert wpilog2json.pick_owlet(index, 19)["Version"] == "26.10.0"
    with pytest.raises(SystemExit):
        wpilog2json.pick_owlet(index, 5)


@pytest.mark.parametrize("system, machine, key", [
    ("Linux", "x86_64", "linuxx86-64"),
    ("Linux", "aarch64", "linuxarm64"),
    ("Darwin", "arm64", "macosuniversal"),
    ("Windows", "AMD64", "windowsx86-64"),
])
def test_owlet_platform(monkeypatch, system, machine, key):
    monkeypatch.setattr(wpilog2json.platform, "system", lambda: system)
    monkeypatch.setattr(wpilog2json.platform, "machine", lambda: machine)
    assert wpilog2json.owlet_platform() == key
