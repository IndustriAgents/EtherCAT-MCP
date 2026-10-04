"""ESI (EtherCAT Slave Information, ETG.2000) parser.

ESI files keep almost everything in child elements, not attributes::

    <EtherCATInfo>
      <Vendor><Id>#x00000002</Id><Name>...</Name></Vendor>
      <Descriptions><Devices>
        <Device>
          <Type ProductCode="#x03f03052" RevisionNo="#x00120000">EL1008</Type>
          <Name LcId="1033">...</Name>
          <Profile><Dictionary>
            <DataTypes><DataType><Name>DT1018</Name><SubItem><SubIdx>1</SubIdx>...</SubItem></DataType></DataTypes>
            <Objects><Object><Index>#x1018</Index><Name>Identity</Name><Type>DT1018</Type>...</Object></Objects>
          </Dictionary></Profile>
          <TxPdo Sm="3"><Index>#x1a00</Index><Name>...</Name>
            <Entry><Index>#x6000</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>...</Name>
              <DataType>BOOL</DataType></Entry>
          </TxPdo>
        </Device>
      </Devices></Descriptions>
    </EtherCATInfo>

Numbers use ``#x`` for hex. ESI files have no use for a DTD, so a file that
declares a DOCTYPE or an ENTITY is refused before parsing: libxml2 expands
internal entities in attribute values even with entity resolution off, which
allows a small file to cost seconds and hundreds of MB (an "entity bomb").
Files larger than the configured cap are refused too. The parser never loads
DTDs, resolves external entities or touches the network.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from lxml import etree

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_DECLARATIONS = (b"<!doctype", b"<!entity")


class ESIParserError(RuntimeError):
    """Raised when an ESI file cannot be found or parsed."""


def _local(el: Any) -> str:
    tag = el.tag
    if not isinstance(tag, str):  # comments, processing instructions
        return ""
    return etree.QName(tag).localname


def _children(el: Any, name: str) -> list[Any]:
    return [child for child in el if _local(child) == name] if el is not None else []


def _child(el: Any, name: str) -> Any | None:
    for child in el if el is not None else ():
        if _local(child) == name:
            return child
    return None


def _text(el: Any, name: str | None = None) -> str | None:
    node = _child(el, name) if name else el
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _number(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    try:
        if text[:2].lower() == "#x":
            return int(text[2:], 16)
        return int(text, 0)
    except ValueError:
        return None


def _hex(raw: str | None, width: int) -> str | None:
    value = _number(raw)
    return None if value is None else f"0x{value:0{width}X}"


def _access(el: Any) -> str | None:
    return _text(_child(el, "Flags"), "Access")


class ESIParser:
    def __init__(self, base_path: Path | str | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.base_path = Path(base_path).expanduser() if base_path else None
        self.max_bytes = max_bytes
        self._cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def resolve(self, filepath: str | Path) -> Path:
        path = Path(filepath).expanduser()
        if path.is_file():
            return path
        if self.base_path:
            candidate = self.base_path / path.name
            if candidate.is_file():
                return candidate
        where = f" (also looked in ETHERCAT_ESI_PATH={self.base_path})" if self.base_path else ""
        raise ESIParserError(f"ESI file not found: {filepath}{where}")

    def load(self, filepath: str | Path) -> dict[str, Any]:
        """Parse a whole ESI file. Results are cached until the file changes."""
        path = self.resolve(filepath)
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        key = str(path.resolve())
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
        parsed = self._parse(path)
        with self._lock:
            self._cache[key] = (stamp, parsed)
        return parsed

    @staticmethod
    def _declares_dtd(data: bytes) -> bool:
        """True if the document declares a DOCTYPE or ENTITY, in UTF-8/Latin-1 or UTF-16/32."""
        texts = [data]
        for bom, codec in (
            (b"\xff\xfe\x00\x00", "utf-32-le"),
            (b"\x00\x00\xfe\xff", "utf-32-be"),
            (b"\xff\xfe", "utf-16-le"),
            (b"\xfe\xff", "utf-16-be"),
        ):
            if data.startswith(bom):
                texts.append(data.decode(codec, errors="replace").encode("utf-8"))
                break
        else:
            if b"\x00" in data[:4]:  # UTF-16/32 without a byte-order mark
                for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
                    texts.append(data.decode(codec, errors="replace").encode("utf-8"))
        return any(marker in text.lower() for text in texts for marker in _DECLARATIONS)

    def _parse(self, path: Path) -> dict[str, Any]:
        try:
            size = path.stat().st_size
            if size > self.max_bytes:
                raise ESIParserError(
                    f"ESI file {path} is {size} bytes; the limit is {self.max_bytes} (ETHERCAT_ESI_MAX_MB)"
                )
            data = path.read_bytes()
        except OSError as exc:
            raise ESIParserError(f"Cannot read ESI file {path}: {exc}") from exc
        if self._declares_dtd(data):
            raise ESIParserError(
                f"Refusing {path}: it declares a DOCTYPE or ENTITY, which ESI files do not use and which "
                "can make parsing explode"
            )
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True
        )
        try:
            root = etree.fromstring(data, parser, base_url=str(path))
        except etree.XMLSyntaxError as exc:
            raise ESIParserError(f"Failed to parse ESI file {path}: {exc}") from exc
        if _local(root) != "EtherCATInfo":
            raise ESIParserError(f"{path} is not an ESI file (root element is <{_local(root)}>, not <EtherCATInfo>)")
        vendor = _child(root, "Vendor")
        devices_el = _child(_child(root, "Descriptions"), "Devices")
        devices = [self._device(dev) for dev in _children(devices_el, "Device")]
        return {
            "filepath": str(path),
            "vendor": {"id": _hex(_text(vendor, "Id"), 8), "name": _text(vendor, "Name")},
            "devices": devices,
        }

    def _device(self, dev: Any) -> dict[str, Any]:
        type_el = _child(dev, "Type")
        names = _children(dev, "Name")
        name_el = next((n for n in names if n.get("LcId") == "1033"), names[0] if names else None)
        dictionary = _child(_child(dev, "Profile"), "Dictionary")
        return {
            "type": _text(type_el),
            "name": _text(name_el),
            "product_code": _hex(type_el.get("ProductCode") if type_el is not None else None, 8),
            "revision": _hex(type_el.get("RevisionNo") if type_el is not None else None, 8),
            "group": _text(dev, "GroupType"),
            "object_dictionary": self._objects(dictionary),
            "pdos": {
                "tx": [self._pdo(pdo) for pdo in _children(dev, "TxPdo")],
                "rx": [self._pdo(pdo) for pdo in _children(dev, "RxPdo")],
            },
        }

    def _objects(self, dictionary: Any) -> list[dict[str, Any]]:
        if dictionary is None:
            return []
        datatypes: dict[str, Any] = {}
        for dt in _children(_child(dictionary, "DataTypes"), "DataType"):
            name = _text(dt, "Name")
            if name:
                datatypes[name] = dt
        objects = []
        for obj in _children(_child(dictionary, "Objects"), "Object"):
            type_name = _text(obj, "Type")
            dt = datatypes.get(type_name or "")
            subitems = []
            for sub in _children(dt, "SubItem"):
                subitems.append(
                    {
                        "subindex": _number(_text(sub, "SubIdx")),
                        "name": _text(sub, "Name"),
                        "type": _text(sub, "Type"),
                        "bit_size": _number(_text(sub, "BitSize")),
                        "bit_offset": _number(_text(sub, "BitOffs")),
                        "access": _access(sub),
                    }
                )
            objects.append(
                {
                    "index": _hex(_text(obj, "Index"), 4),
                    "name": _text(obj, "Name"),
                    "type": type_name,
                    "bit_size": _number(_text(obj, "BitSize")),
                    "access": _access(obj),
                    "subindexes": subitems,
                }
            )
        return objects

    @staticmethod
    def _pdo(pdo: Any) -> dict[str, Any]:
        sm = pdo.get("Sm")
        return {
            "index": _hex(_text(pdo, "Index"), 4),
            "name": _text(pdo, "Name"),
            "sm": _number(sm) if sm is not None else None,
            "fixed": pdo.get("Fixed") in ("1", "true"),
            "mandatory": pdo.get("Mandatory") in ("1", "true"),
            "entries": [
                {
                    "index": _hex(_text(entry, "Index"), 4),
                    "subindex": _number(_text(entry, "SubIndex")),
                    "bit_length": _number(_text(entry, "BitLen")),
                    "name": _text(entry, "Name"),
                    "data_type": _text(entry, "DataType"),
                }
                for entry in _children(pdo, "Entry")
            ],
        }

    def select(
        self, filepath: str | Path, device_type: str | None = None, revision: str | None = None
    ) -> dict[str, Any]:
        """Load ``filepath`` and pick devices.

        One matching device comes back with its object dictionary and PDOs.
        Several matching devices come back as a summary list, so a vendor file
        with dozens of terminals does not flood the client.
        """
        doc = self.load(filepath)
        devices = doc["devices"]
        if device_type:
            devices = [d for d in devices if (d["type"] or "").lower() == device_type.strip().lower()]
        if revision:
            wanted = _number(revision)
            if wanted is None:
                raise ESIParserError(f"revision {revision!r} is not a number (use hex such as 0x00120000)")
            devices = [d for d in devices if d["revision"] is not None and _number(d["revision"]) == wanted]
        result: dict[str, Any] = {
            "filepath": doc["filepath"],
            "vendor": doc["vendor"],
            "device_count": len(doc["devices"]),
            "matched": len(devices),
        }
        if not devices:
            available = sorted({f"{d['type']} {d['revision']}" for d in doc["devices"]})
            raise ESIParserError(
                f"No device in {doc['filepath']} matches"
                f"{' type ' + device_type if device_type else ''}{' revision ' + revision if revision else ''}. "
                f"Available: {', '.join(available[:20]) or 'none'}{' ...' if len(available) > 20 else ''}"
            )
        if len(devices) == 1:
            result["devices"] = devices
        else:
            result["devices"] = [
                {k: d[k] for k in ("type", "name", "product_code", "revision", "group")} for d in devices
            ]
            result["hint"] = "Several devices match; pass device_type (and revision) to get one device's details."
        return result
