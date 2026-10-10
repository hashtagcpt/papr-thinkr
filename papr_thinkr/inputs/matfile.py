"""MATLAB .mat files: a summary of every variable (class, size, numeric
statistics and NaN counts, struct fields, cell contents) for the reviewer.

v4/v5/v6/v7 files (MATLAB's default `save` format) are parsed with the
standard library alone. v7.3 files are HDF5 and need h5py. SciPy, when
installed, is used as a fallback for anything the built-in reader can't
handle."""
from __future__ import annotations

import array
import math
import struct
import zlib
from collections import Counter
from pathlib import Path

_MI = {1: ("b", 1), 2: ("B", 1), 3: ("h", 2), 4: ("H", 2), 5: ("i", 4), 6: ("I", 4), 7: ("f", 4),
       9: ("d", 8), 12: ("q", 8), 13: ("Q", 8), 16: ("B", 1), 17: ("H", 2), 18: ("I", 4)}
_MI_MATRIX, _MI_COMPRESSED = 14, 15
_CLASSES = {1: "cell", 2: "struct", 3: "object", 4: "char", 5: "sparse", 6: "double", 7: "single",
            8: "int8", 9: "uint8", 10: "int16", 11: "uint16", 12: "int32", 13: "uint32", 14: "int64",
            15: "uint64", 16: "function"}
_NUMERIC = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
_MAX_STATS_VALUES = 5_000_000
_MAX_DEPTH = 3


class MatError(ValueError):
    pass


def _numbers(mi_type: int, data: bytes, endian: str) -> list:
    if mi_type not in _MI:
        raise MatError(f"unsupported data type {mi_type}")
    code, size = _MI[mi_type]
    n = len(data) // size
    for c in (code, {4: "l", 8: "q"}.get(size, code)):
        if array.array(c).itemsize == size:
            a = array.array(c)
            a.frombytes(data[:n * size])
            if endian == ">":
                a.byteswap()
            return a.tolist() if n <= _MAX_STATS_VALUES else a[::max(1, n // _MAX_STATS_VALUES)].tolist()
    return list(struct.unpack(f"{endian}{n}{code}", data[:n * size]))


def _stats(values: list) -> dict:
    finite = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    nan = sum(1 for v in values if isinstance(v, float) and math.isnan(v))
    out = {"n": len(values), "nan": nan}
    if finite:
        mean = sum(finite) / len(finite)
        var = sum((v - mean) ** 2 for v in finite) / len(finite)
        out.update(min=_r(min(finite)), max=_r(max(finite)), mean=_r(mean), sd=_r(math.sqrt(var)))
        uniq = set(finite[:20000])
        if len(uniq) <= 12:
            out["distinct_values"] = sorted(uniq)
    return out


def _r(x: float) -> float:
    return float(f"{x:.5g}")


class _Stream:
    def __init__(self, data: bytes, endian: str):
        self.d, self.e, self.pos = data, endian, 0

    def element(self) -> tuple[int, bytes] | None:
        if self.pos + 8 > len(self.d):
            return None
        first, nbytes = struct.unpack_from(self.e + "II", self.d, self.pos)
        if first >> 16:                       # "small data element": type+size in 4 bytes, data in next 4
            size, mi = first >> 16, first & 0xFFFF
            data = self.d[self.pos + 4:self.pos + 4 + size]
            self.pos += 8
            return mi, data
        mi = first
        data = self.d[self.pos + 8:self.pos + 8 + nbytes]
        self.pos += 8 + nbytes
        if mi != _MI_COMPRESSED:
            self.pos += (8 - nbytes % 8) % 8   # elements are padded to 8 bytes
        return mi, data


def _char_text(mi: int, data: bytes, dims: tuple, e: str) -> list[str]:
    if mi == 16:
        chars = data.decode("utf-8", "replace")
    elif mi in (17, 4):
        chars = data.decode("utf-16-le" if e == "<" else "utf-16-be", "replace")
    elif mi in (18,):
        chars = data.decode("utf-32-le" if e == "<" else "utf-32-be", "replace")
    else:
        chars = "".join(chr(c) for c in _numbers(mi, data, e) if 0 <= c < 0x110000)
    rows = dims[0] if dims else 1
    if rows <= 1:
        return [chars]
    cols = len(chars) // rows
    return ["".join(chars[c * rows + r] for c in range(cols)).rstrip() for r in range(rows)]


def _matrix(data: bytes, e: str, depth: int = 0) -> dict:
    if not data:
        return {"class": "empty", "shape": [0, 0]}
    s = _Stream(data, e)
    _mi, flags = s.element()
    f0 = struct.unpack(e + "I", flags[:4])[0]
    cls = f0 & 0xFF
    out: dict = {"class": _CLASSES.get(cls, f"class{cls}")}
    if f0 & 0x0800:
        out["complex"] = True
    if f0 & 0x0200:
        out["class"] = "logical"
    _mi, dims_b = s.element()
    dims = struct.unpack(f"{e}{len(dims_b) // 4}i", dims_b)
    out["shape"] = list(dims)
    _mi, name_b = s.element()
    out["name"] = name_b.decode("latin-1").rstrip("\x00")
    count = 1
    for d in dims:
        count *= d
    if cls in _NUMERIC:
        mi, pr = s.element()
        out["stats"] = _stats(_numbers(mi, pr, e))
    elif cls == 4:
        mi, chars = s.element()
        texts = _char_text(mi, chars, dims, e)
        out["text"] = texts[:20]
    elif cls == 5:
        s.element(), s.element()
        mi, pr = s.element()
        out["nnz"] = len(pr) // _MI.get(mi, ("d", 8))[1]
        out["stats"] = _stats(_numbers(mi, pr, e))
    elif cls == 1:
        cells = []
        for _ in range(min(count, 5000)):
            el = s.element()
            if el is None:
                break
            cells.append(_matrix(el[1], e, depth + 1) if depth < _MAX_DEPTH else {"class": "..."})
        out["cell_classes"] = dict(Counter(c["class"] for c in cells))
        texts = [t for c in cells for t in c.get("text", [])]
        if texts:
            out["cell_text_counts"] = dict(Counter(texts).most_common(12))
        elif cells and depth < _MAX_DEPTH:
            out["first_cell"] = cells[0]
    elif cls in (2, 3):
        if cls == 3:
            _mi, cname = s.element()
            out["object_class"] = cname.decode("latin-1").rstrip("\x00")
        _mi, fl = s.element()
        flen = struct.unpack(e + "i", fl[:4])[0]
        _mi, names_b = s.element()
        names = [names_b[i:i + flen].split(b"\x00")[0].decode("latin-1") for i in range(0, len(names_b), flen)]
        out["fields"] = names
        if depth < _MAX_DEPTH:
            firsts = {}
            for _ in range(min(count, 1)):
                for n in names:
                    el = s.element()
                    if el is None:
                        break
                    firsts[n] = _matrix(el[1], e, depth + 1)
            out["field_values"] = firsts
    return out


def _read_v5(raw: bytes) -> list[dict]:
    endian = "<" if raw[126:128] == b"IM" else ">"
    s = _Stream(raw, endian)
    s.pos = 128
    variables = []
    while True:
        el = s.element()
        if el is None:
            break
        mi, data = el
        if mi == _MI_COMPRESSED:
            data = zlib.decompress(data)
            inner = _Stream(data, endian).element()
            if inner is None:
                continue
            mi, data = inner
        if mi == _MI_MATRIX:
            variables.append(_matrix(data, endian))
    return variables


def _read_scipy(path: Path) -> list[dict]:
    import numpy as np
    from scipy.io import loadmat
    m = loadmat(str(path), squeeze_me=False, struct_as_record=True)
    out = []
    for name, v in m.items():
        if name.startswith("__"):
            continue
        d = {"name": name, "class": str(v.dtype), "shape": list(v.shape)}
        if np.issubdtype(v.dtype, np.number):
            d["stats"] = _stats([float(x) for x in np.ravel(v)[:_MAX_STATS_VALUES]])
        elif v.dtype.names:
            d["class"], d["fields"] = "struct", list(v.dtype.names)
        out.append(d)
    return out


def _read_h5(path: Path) -> list[dict]:
    import h5py
    import numpy as np
    out = []
    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and not name.startswith("#"):
                d = {"name": name, "class": obj.attrs.get("MATLAB_class", b"").decode() or str(obj.dtype),
                     "shape": list(obj.shape)[::-1]}
                if np.issubdtype(obj.dtype, np.number) and obj.size <= 50_000_000:
                    d["stats"] = _stats([float(x) for x in np.ravel(obj[()])[:_MAX_STATS_VALUES]])
                out.append(d)
        f.visititems(visit)
    return out


def summarize_mat(path: str | Path) -> dict:
    """{"format": ..., "variables": [...]} or {"error"/"note": ...}."""
    p = Path(path)
    raw = p.read_bytes()
    head = raw[:116].decode("latin-1", "replace")
    if raw[512:520] == b"\x89HDF\r\n\x1a\n" or "MATLAB 7.3" in head:
        try:
            return {"format": "MAT v7.3 (HDF5)", "variables": _read_h5(p)}
        except ImportError:
            return {"format": "MAT v7.3 (HDF5)",
                    "note": "HDF5-based v7.3 file: install h5py (pip install h5py) to inspect it, or re-save it in "
                            "MATLAB with save(..., '-v7')."}
    if head.startswith("MATLAB 5.0") or raw[126:128] in (b"IM", b"MI"):
        try:
            return {"format": "MAT v5/v7", "variables": _read_v5(raw)}
        except Exception as e:   # unusual content: fall back to SciPy if it's there
            try:
                return {"format": "MAT v5/v7", "variables": _read_scipy(p)}
            except Exception:
                return {"format": "MAT v5/v7", "error": f"could not parse: {type(e).__name__}: {e}"}
    try:
        return {"format": "MAT v4", "variables": _read_scipy(p)}
    except Exception:
        return {"error": "not a recognised MAT file (v4 files need SciPy)"}


def describe_variable(v: dict, indent: str = "  - ") -> list[str]:
    shape = "×".join(str(x) for x in v.get("shape", [])) or "?"
    line = f"{indent}{v.get('name') or '(unnamed)'}: {v.get('class')} {shape}"
    st = v.get("stats")
    if st and "mean" in st:
        line += f", min={st['min']} max={st['max']} mean={st['mean']} sd={st['sd']}"
        if st.get("distinct_values"):
            line += f", values {st['distinct_values']}"
    if st and st.get("nan"):
        line += f", NaN={st['nan']} ({100 * st['nan'] / max(1, st['n']):.1f}%)"
    if v.get("text"):
        line += f", text {v['text'][:4]}"
    if v.get("cell_text_counts"):
        line += f", contents {v['cell_text_counts']}"
    elif v.get("cell_classes"):
        line += f", cells {v['cell_classes']}"
    if v.get("fields"):
        line += f", fields {v['fields'][:20]}"
    lines = [line]
    for name, fv in (v.get("field_values") or {}).items():
        fv = dict(fv, name=f"{v.get('name')}.{name}")
        lines.extend(describe_variable(fv, indent="    " + indent.strip() + " "))
    return lines
