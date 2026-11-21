"""Protocol Buffers types from the official Diadoc SDK schemas."""

from __future__ import annotations

from collections import defaultdict
from importlib.resources import files

from google.protobuf import descriptor_pool, message_factory
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper
from google.protobuf.message import Message as _Message


def _load_schemas() -> tuple[descriptor_pool.DescriptorPool, dict[str, list[str]]]:
    data = files(__package__).joinpath("_diadoc.pb").read_bytes()
    descriptors = FileDescriptorSet.FromString(data)
    pool = descriptor_pool.DescriptorPool()
    names: dict[str, list[str]] = defaultdict(list)
    for file in descriptors.file:
        pool.AddSerializedFile(file.SerializeToString())
        for item in (*file.message_type, *file.enum_type):
            names[item.name].append(f"{file.package}.{item.name}")
    return pool, names


_POOL, _NAMES = _load_schemas()


def _full_name(name: str) -> str:
    if "." in name:
        return name
    candidates = _NAMES.get(name, [])
    if len(candidates) > 1:
        raise KeyError(f"Ambiguous Protobuf name {name!r}. Use one of: {', '.join(candidates)}")
    if not candidates:
        raise KeyError(f"Unknown Protobuf name {name!r}")
    return candidates[0]


def get_message(name: str) -> type[_Message]:
    """Return a message class by its full or unique short name."""
    return message_factory.GetMessageClass(_POOL.FindMessageTypeByName(_full_name(name)))


def get_enum(name: str) -> EnumTypeWrapper:
    """Return an enumeration by its full or unique short name."""
    return EnumTypeWrapper(_POOL.FindEnumTypeByName(_full_name(name)))


def __getattr__(name: str) -> type[_Message] | EnumTypeWrapper:
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        full_name = _full_name(name)
        try:
            return get_message(full_name)
        except KeyError:
            return get_enum(full_name)
    except KeyError as error:
        raise AttributeError(error.args[0]) from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | {name for name, candidates in _NAMES.items() if len(candidates) == 1})
