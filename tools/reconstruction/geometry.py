"""Validate the geometry and local material references emitted by OpenMVS."""

import math
import struct
import zlib
from pathlib import Path

from tools.capture.validate import require, safe_path

PLY_TYPES = {
    "char": "b",
    "uchar": "B",
    "short": "h",
    "ushort": "H",
    "int": "i",
    "uint": "I",
    "float": "f",
    "double": "d",
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "float32": "f",
    "float64": "d",
}


def validate_texture(path):
    """Check the PNG/JPEG container and dimensions emitted by the texture adapter."""
    if Path(path).read_bytes()[:2] == b"\xff\xd8":
        return validate_jpeg(path)
    with Path(path).open("rb") as stream:
        require(stream.read(8) == b"\x89PNG\r\n\x1a\n", "expected PNG texture")
        header, compressed, ended = None, bytearray(), False
        while not ended:
            prefix = stream.read(8)
            require(len(prefix) == 8, "truncated PNG chunk")
            length, kind = struct.unpack(">I4s", prefix)
            require(length <= 268435456, "oversized PNG chunk")
            payload, checksum = stream.read(length), stream.read(4)
            require(
                len(payload) == length
                and len(checksum) == 4
                and zlib.crc32(kind + payload) & 0xFFFFFFFF == struct.unpack(">I", checksum)[0],
                "invalid PNG checksum",
            )
            if kind == b"IHDR":
                require(header is None and length == 13, "invalid PNG header")
                header = struct.unpack(">IIBBBBB", payload)
            elif kind == b"IDAT":
                compressed.extend(payload)
            elif kind == b"IEND":
                require(length == 0, "invalid PNG end")
                ended = True
        require(header is not None and not stream.read(), "incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    require(
        0 < width <= 65536
        and 0 < height <= 65536
        and depth in (1, 2, 4, 8, 16)
        and color in channels
        and compression == filtering == interlace == 0,
        "unsupported texture PNG layout",
    )
    stride = (width * channels[color] * depth + 7) // 8 + 1
    expected = stride * height
    require(expected <= 1073741824, "texture exceeds validation memory limit")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected + 1)
    require(len(raw) == expected and decoder.eof and not decoder.unused_data, "PNG scanline size mismatch")
    require(all(raw[offset] <= 4 for offset in range(0, expected, stride)), "invalid PNG scanline filter")
    return {"width": width, "height": height}


def validate_jpeg(path):
    data = Path(path).read_bytes()
    cursor, dimensions, scans = 2, None, 0
    while cursor < len(data):
        require(data[cursor] == 255, "invalid JPEG marker boundary")
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        require(cursor < len(data), "truncated JPEG marker")
        marker = data[cursor]
        cursor += 1
        if marker == 0xD9:
            require(cursor == len(data) and dimensions is not None and scans > 0, "incomplete JPEG image")
            return dimensions
        require(marker not in (0, 0xD8) and cursor + 2 <= len(data), "invalid JPEG segment")
        size = int.from_bytes(data[cursor : cursor + 2], "big")
        require(size >= 2 and cursor + size <= len(data), "truncated JPEG segment")
        if marker in (0xC0, 0xC1, 0xC2):
            require(size >= 8, "invalid JPEG frame header")
            height, width = struct.unpack_from(">HH", data, cursor + 3)
            require(width > 0 and height > 0, "invalid JPEG dimensions")
            dimensions = {"width": width, "height": height}
        cursor += size
        if marker == 0xDA:
            scans += 1
            while cursor < len(data):
                boundary = data.find(b"\xff", cursor)
                require(boundary >= 0 and boundary + 1 < len(data), "truncated JPEG scan")
                following = data[boundary + 1]
                if following == 0 or 0xD0 <= following <= 0xD7:
                    cursor = boundary + 2
                else:
                    cursor = boundary
                    break
    require(False, "JPEG end marker is missing")


def validate_ply(path, mesh=False):
    """Parse scalar/list PLY records, including binary files, without loading the cloud."""
    with Path(path).open("rb") as stream:
        require(stream.readline() == b"ply\n", "invalid PLY signature")
        elements, encoding = [], None
        while True:
            line = stream.readline(65536)
            require(line and len(line) < 65536, "truncated PLY header")
            words = line.decode("ascii").split()
            if words[0] == "format":
                encoding = words[1]
                require(words[2] == "1.0", "unsupported PLY version")
            elif words[0] == "element":
                count = int(words[2])
                require(0 <= count <= 1000000000, "invalid PLY element count")
                elements.append({"name": words[1], "count": count, "properties": []})
            elif words[0] == "property":
                require(bool(elements), "PLY property without element")
                types = words[2:4] if words[1] == "list" else words[1:2]
                require(all(value in PLY_TYPES for value in types), "unsupported PLY property type")
                elements[-1]["properties"].append((words[-1], types))
            elif words[0] == "end_header":
                break
        require(encoding in ("ascii", "binary_little_endian", "binary_big_endian"), "unsupported PLY encoding")
        counts = {element["name"]: element["count"] for element in elements}
        require(counts.get("vertex", 0) > 0 and (not mesh or counts.get("face", 0) > 0), "empty PLY geometry")
        endian = "<" if encoding == "binary_little_endian" else ">"
        for element in elements:
            if element["name"] == "vertex":
                require(
                    {"x", "y", "z"}.issubset({name for name, _ in element["properties"]}),
                    "PLY lacks vertex coordinates",
                )
            for _ in range(element["count"]):
                values = iter(stream.readline().decode("ascii").split()) if encoding == "ascii" else None

                def scalar(kind, values=values):
                    if values is not None:
                        token = next(values)
                        value = float(token) if PLY_TYPES[kind] in ("f", "d") else int(token)
                    else:
                        code = PLY_TYPES[kind]
                        data = stream.read(struct.calcsize(code))
                        require(len(data) == struct.calcsize(code), "truncated binary PLY")
                        value = struct.unpack(endian + code, data)[0]
                    require(math.isfinite(value), "non-finite PLY value")
                    return value

                for name, types in element["properties"]:
                    if len(types) == 1:
                        scalar(types[0])
                    else:
                        count = scalar(types[0])
                        require(isinstance(count, int) and 0 <= count <= 10000000, "invalid PLY list count")
                        indices = [scalar(types[1]) for _ in range(count)]
                        if element["name"] == "face" and name in ("vertex_indices", "vertex_index"):
                            require(
                                len(indices) >= 3
                                and all(isinstance(index, int) and 0 <= index < counts["vertex"] for index in indices),
                                "invalid PLY face indices",
                            )
                if values is not None:
                    require(next(values, None) is None, "extra ASCII PLY columns")
        require(not stream.read().strip(), "trailing PLY payload")
    return counts


def validate_obj(path):
    root = Path(path).parent
    counts = {"v": 0, "vt": 0, "vn": 0, "faces": 0}
    materials, textured, used, references = set(), set(), set(), set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        kind = words[0]
        if kind in ("v", "vt", "vn"):
            values = [float(value) for value in words[1:]]
            require(
                len(values) >= (2 if kind == "vt" else 3) and all(math.isfinite(value) for value in values),
                "invalid OBJ coordinates",
            )
            counts[kind] += 1
        elif kind == "f":
            require(len(words) >= 4, "invalid OBJ face")
            for vertex in words[1:]:
                parts = vertex.split("/")
                require(2 <= len(parts) <= 3 and bool(parts[1]), "OBJ face lacks texture coordinates")
                for index, value in enumerate(parts):
                    if value:
                        number = int(value)
                        count = counts[("v", "vt", "vn")[index]]
                        require(number != 0 and -count <= number <= count, "OBJ index is outside geometry")
            counts["faces"] += 1
        elif kind == "usemtl":
            used.add(" ".join(words[1:]))
        elif kind == "mtllib":
            require(len(words) == 2, "OBJ material filenames with spaces are unsupported")
            material = safe_path(root, words[1])
            references.add(material)
            current_material = None
            for entry in material.read_text(encoding="utf-8").splitlines():
                fields = entry.split()
                if not fields:
                    continue
                if fields[0] == "newmtl":
                    current_material = " ".join(fields[1:])
                    materials.add(current_material)
                elif fields[0] == "map_Kd":
                    require(len(fields) == 2, "unsupported OBJ texture options")
                    texture = safe_path(root, fields[1])
                    require(current_material is not None, "texture without material")
                    validate_texture(texture)
                    textured.add(current_material)
                    references.add(texture)
    require(counts["v"] > 0 and counts["faces"] > 0 and counts["vt"] > 0, "OBJ lacks textured mesh geometry")
    require(
        bool(used) and used.issubset(materials & textured) and bool(references),
        "OBJ material references are incomplete",
    )
    return {"counts": counts, "references": sorted(references)}
