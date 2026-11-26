"""Read package and versionName from an APK's binary AndroidManifest.xml.

Lets the runner identify an APK on hosts without aapt. Built APKs store the
manifest in Android's binary XML format, so text matching can't work.
"""
import struct
import zipfile

_CHUNK_STRING_POOL = 0x0001
_CHUNK_START_ELEMENT = 0x0102
_TYPE_STRING = 0x03
_UTF8_FLAG = 0x100
_NO_INDEX = 0xFFFFFFFF


def _read_strings(data, start):
    count, _styles, flags, strings_start = struct.unpack_from("<IIII", data, start + 8)
    offsets = struct.unpack_from(f"<{count}I", data, start + 28)
    base = start + strings_start
    strings = []
    for offset in offsets:
        pos = base + offset
        if flags & _UTF8_FLAG:
            # Two variable-length prefixes: UTF-16 length, then UTF-8 byte length.
            pos += 2 if data[pos] & 0x80 else 1
            length = data[pos]
            if length & 0x80:
                length = ((length & 0x7F) << 8) | data[pos + 1]
                pos += 1
            pos += 1
            strings.append(data[pos:pos + length].decode("utf-8", errors="replace"))
        else:
            length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos)[0]
                pos += 2
            strings.append(data[pos:pos + length * 2].decode("utf-16-le", errors="replace"))
    return strings


def parse_manifest_attributes(data):
    """Return the <manifest> element's string attributes as a dict."""
    strings = []
    pos = struct.unpack_from("<H", data, 2)[0]
    while pos + 8 <= len(data):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, pos)
        if chunk_size < 8:
            break
        if chunk_type == _CHUNK_STRING_POOL:
            strings = _read_strings(data, pos)
        elif chunk_type == _CHUNK_START_ELEMENT:
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, pos + header_size + 8)
            first = pos + header_size + attr_start
            attrs = {}
            for i in range(attr_count):
                _ns, name, raw, _size, _res, value_type, value = struct.unpack_from(
                    "<IIIHBBI", data, first + i * attr_size)
                index = raw if raw != _NO_INDEX else (value if value_type == _TYPE_STRING else None)
                if index is not None and name < len(strings) and index < len(strings):
                    attrs[strings[name]] = strings[index]
            return attrs  # the first element of a manifest is <manifest>
        pos += chunk_size
    return {}


def read_apk_identity(apk_path):
    """Return (package, version_name), either None if unavailable."""
    with zipfile.ZipFile(apk_path) as apk:
        attrs = parse_manifest_attributes(apk.read("AndroidManifest.xml"))
    return attrs.get("package"), attrs.get("versionName")
