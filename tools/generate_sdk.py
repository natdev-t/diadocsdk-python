"""Generate the public methods and Protobuf descriptor set from upstream sources."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

from google.protobuf.descriptor_pb2 import FileDescriptorSet

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "diadoc"
JSON = "application/json"
BINARY = "application/octet-stream"
TEXT = "text/plain"
XML = "application/xml"
PROTO = "Diadoc.Api.Proto."
DOCUMENT_PARAMS = ("boxId", "messageId", "documentId")
LEGACY_AUTH = {"/V3/Authenticate", "/V3/AuthenticateConfirm"}
PRESENCE_FLAGS = {"disableValidation", "excludeSubdepartments", "originalSignature"}

# Published methods that were absent from the OpenAPI export.
# Values are request and response schema names (or MIME types), in that order.
ADDITIONS = {
    ("get", "/V3/AcquireCounteragentResult"): (
        ("myBoxId", "taskId"), {}, None, "AcquireCounteragentResultV2",
    ),
    ("post", "/admin/CreateDepartment"): (
        ("boxId",), {}, "DepartmentToCreate", PROTO + "Departments.Department",
    ),
    ("post", "/admin/DeleteDepartment"): (("boxId", "departmentId"), {}, None, None),
    ("get", "/admin/GetDepartments"): (("boxId",), {"page": "integer", "count": "integer"}, None, "DepartmentList"),
    ("post", "/admin/UpdateDepartment"): (
        ("boxId", "departmentId"), {}, "DepartmentToUpdate", PROTO + "Departments.Department",
    ),
    ("post", "/GeneratePrintFormPng"): (DOCUMENT_PARAMS, {}, None, "AsyncMethodResult"),
    ("post", "/GeneratePrintFormPngFromAttachment"): (
        ("boxId", "documentType"), {}, BINARY, "AsyncMethodResult",
    ),
    ("get", "/GetGeneratedPrintFormResult"): (("boxId", "taskId"), {}, None, "GeneratedPrintFormResult"),
    ("post", "/GenerateTtGisFixationCancellationRequest"): (DOCUMENT_PARAMS, {}, None, JSON),
    ("get", "/GetDocumentHistory"): (
        ("boxId", "messageId", "entityId"), {"embed": "array"}, None, "DocumentHistory",
    ),
    ("get", "/V2/GetWorkflowsSettings"): (("boxId",), {}, None, "DocumentWorkflowSettingsListV2"),
    ("get", "/V1/ParseGarAddress"): (("address",), {"isAdministrativeDivision": "boolean"}, None, "GarAddress"),
}

NAMES = {
    ("get", "/V1/GetSecurityEvents"): "GetSecurityEvents",
    ("post", "/DssSign"): "DssSignV1",
}

RESPONSE_MEDIA = {
    "/ParseUniversalMessage": XML,
    "/ParseUniversalMessageXml": XML,
    "/V3/Authenticate": TEXT,
    "/ShelfUploadPart": JSON,
}

MODEL_NAMES = {
    "Department": PROTO + "Department",
    "Employee": PROTO + "Employees.Employee",
    "InvoicingRevocationRequestInfo": PROTO + "Invoicing.RevocationRequestInfo",
    "ResolutionChainList": PROTO + "ResolutionRouteList",
}

RETRYABLE_EMPTY = {
    "/GenerateDocumentProtocol",
    "/GenerateDocumentZip",
    "/GeneratePrintForm",
    "/GetGeneratedPrintForm",
    "/RegisterPowerOfAttorneyResult",
    "/ShelfUploadPart",
    "/V3/AcquireCounteragentResult",
}


def snake_case(name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def compile_descriptors(archive: Path, protoc: str) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(archive) as upstream:
        working = Path(directory)
        for name in upstream.namelist():
            parts = name.split("/")
            if len(parts) > 2 and parts[1] == "proto" and name.endswith(".proto"):
                destination = working.joinpath(*parts[2:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(upstream.read(name))
        for source in (ROOT / "tools" / "proto").glob("*.proto"):
            destination = working / source.name
            if not destination.exists():
                destination.write_bytes(source.read_bytes())
        descriptor = working / "diadoc.pb"
        paths = sorted(path.relative_to(working).as_posix() for path in working.rglob("*.proto"))
        subprocess.run(
            [protoc, "--proto_path=.", "--include_imports", f"--descriptor_set_out={descriptor}", *paths],
            cwd=working,
            check=True,
        )
        return descriptor.read_bytes(), upstream.comment.decode("ascii") or archive.name


def model_names(descriptor: bytes) -> dict[str, str]:
    names = defaultdict(list)
    for file in FileDescriptorSet.FromString(descriptor).file:
        for message in file.message_type:
            names[message.name].append(f"{file.package}.{message.name}")
    full_names = {full_name: full_name for entries in names.values() for full_name in entries}
    short_names = {name: entries[0] for name, entries in names.items() if len(entries) == 1}
    return full_names | short_names | MODEL_NAMES


def content(type_name: str | None) -> dict:
    if type_name is None:
        return {}
    if "/" in type_name:
        return {type_name: {}}
    return {JSON: {"schema": {"$ref": f"#/components/schemas/{type_name}"}}}


def operations(spec: dict) -> dict[tuple[str, str], dict]:
    result = {(method, path): operation for path, methods in spec["paths"].items() for method, operation in methods.items()}
    for (method, path), (required, optional, body, response) in ADDITIONS.items():
        result.setdefault((method, path), {
            "operationId": path.rsplit("/", 1)[-1],
            "parameters": [
                {"name": name, "in": "query", "required": True, "schema": {"type": "string"}} for name in required
            ] + [
                {"name": name, "in": "query", "schema": {"type": type_name}} for name, type_name in optional.items()
            ],
            "requestBody": {"content": content(body), "required": body is not None},
            "responses": {"200": {"content": content(response)}},
        })
    return result


def json_model(media: dict, names: dict[str, str]) -> str | None:
    reference = media.get(JSON, {}).get("schema", {}).get("$ref")
    return names[reference.rsplit("/", 1)[-1]] if reference else None


def generate_methods(spec: dict, descriptor: bytes, revision: str) -> str:
    names = model_names(descriptor)
    methods = {}
    aliases = {}
    for (method, path), operation in operations(spec).items():
        operation_name = NAMES.get((method, path), operation["operationId"])
        match = re.search(r"V(\d+)$", operation_name) or re.match(r"/V(\d+)/", path)
        version = int(match[1]) if match else 0
        base_name = snake_case(re.sub(r"V\d+$", "", operation_name))
        name = f"{base_name}_v{version}" if version else base_name
        if name in methods:
            raise ValueError(f"Duplicate Python method: {name}")
        if version > aliases.get(base_name, 0):
            aliases[base_name] = version
        methods[name] = (method, path, operation)

    output = [
        "# Generated by tools/generate_sdk.py.",
        f"# OpenAPI {spec['info']['version']}; diadocsdk-csharp {revision}.",
        "from __future__ import annotations",
        "",
        "from collections.abc import Sequence",
        "from typing import Any",
        "",
        "from google.protobuf.message import Message",
        "from httpx import Response",
        "",
        "from ._client import _AsyncClient, _Client, StructuredBody",
        "",
        "",
        "def _csv(value: str | Sequence[str] | None) -> str | None:",
        "    if isinstance(value, str):",
        "        return value",
        '    return ",".join(value) if value else None',
        "",
        "",
    ]
    alias_lines = []
    for base_name, version in sorted(aliases.items()):
        if base_name in methods:
            raise ValueError(f"Alias would shadow a Python method: {base_name}")
        alias_lines.append(f"    {base_name} = {base_name}_v{version}")
    for class_name, base_class, mode, asynchronous in (
        ("DiadocClient", "_Client", "Synchronous", False),
        ("AsyncDiadocClient", "_AsyncClient", "Asynchronous", True),
    ):
        if asynchronous:
            output.extend(["", ""])
        output.extend([
            f"class {class_name}({base_class}):",
            f'    """{mode} client for the Kontur.Diadoc HTTP API."""',
            "",
        ])
        for name, (method, path, operation) in sorted(methods.items()):
            output.extend(generate_method(name, method, path, operation, names, asynchronous=asynchronous))
        output.extend(alias_lines)
    return "\n".join(output) + "\n"


def generate_method(
    name: str, method: str, path: str, operation: dict, names: dict[str, str], *, asynchronous: bool,
) -> list[str]:
    required = []
    optional = []
    query = []
    headers = []
    flags = []
    for parameter in operation.get("parameters", ()):
        wire_name = parameter["name"]
        location = parameter["in"]
        if location not in {"query", "header"}:
            raise ValueError(f"Unsupported {location} parameter in {name}")
        python_name = snake_case(wire_name.removeprefix("X-Diadoc-"))
        if path == "/V3/Authenticate" and wire_name == "type":
            python_name = "auth_type"
        type_name = parameter["schema"].get("type", "string")
        python_type = {"integer": "int", "boolean": "bool", "string": "str", "array": "str | Sequence[str]"}[type_name]
        declaration = f"{python_name}: {python_type}"
        if parameter.get("required"):
            required.append(declaration)
        else:
            optional.append(f"{declaration} | None = None")
        expression = f"_csv({python_name})" if type_name == "array" else python_name
        (headers if location == "header" else query).append(f"{wire_name!r}: {expression}")
        if wire_name in PRESENCE_FLAGS:
            flags.append(wire_name)

    request = operation.get("requestBody", {})
    request_media = request.get("content", {})
    body_model = json_model(request_media, names)
    if request_media:
        types = [type_name for mime, type_name in ((JSON, "StructuredBody"), (BINARY, "bytes"), (TEXT, "str"))
                 if mime in request_media]
        if request.get("required") or path == "/UpdateSubscriptions":
            required.append(f"body: {' | '.join(types)}")
        else:
            optional.append(f"body: {' | '.join(types)} | None = None")

    responses = operation["responses"]
    response_content = responses["200"].get("content", {})
    response_media = RESPONSE_MEDIA.get(path, next(iter(response_content), None))
    response_model = json_model(response_content, names)
    if response_model:
        result = ["dict[str, Any]", "Message"]
    elif path == "/V3/Authenticate":
        result = ["str", "bytes"]
    elif path == "/ShelfUploadPart":
        result = ["list[int]"]
    elif response_media == JSON:
        result = ["dict[str, Any]"]
    elif response_media == TEXT:
        result = ["str"]
    else:
        result = ["bytes"] if response_media else []
    result.append("Response")
    if not response_media or "204" in responses or path in RETRYABLE_EMPTY:
        result.append("None")

    signature = ["self", *required, "*", *optional, "raw: bool = False"]
    definition = "async def" if asynchronous else "def"
    await_prefix = "await " if asynchronous else ""
    output = [f"    {definition} {name}(", *(f"        {parameter}," for parameter in signature)]
    output.extend([
        f"    ) -> {' | '.join(result)}:",
        f'        """Call {method.upper()} {path}."""',
        f"        return {await_prefix}self._call(",
        f"            {method.upper()!r}, {path!r},",
    ])
    for field, values in (("params", query), ("headers", headers)):
        if values:
            output.append(f"            {field}={{")
            output.extend(f"                {value}," for value in values)
            output.append("            },")
    if request_media:
        output.append("            body=body,")
    if body_model:
        output.append(f"            body_model={body_model!r},")
    if response_model:
        output.append(f"            response_model={response_model!r},")
    elif path == "/V3/Authenticate":
        output.append(f"            response_media={BINARY!r} if auth_type == 'certificate' else {TEXT!r},")
    elif path == "/ShelfUploadPart":
        output.append(f"            response_media={JSON!r} if is_last_part else None,")
    elif response_media:
        output.append(f"            response_media={response_media!r},")
    if flags:
        output.append(f"            flags={tuple(flags)!r},")
    if path in LEGACY_AUTH:
        output.append("            legacy=True,")
    output.extend(["            raw=raw,", "        )", ""])
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("openapi", type=Path, help="Diadoc OpenAPI JSON export")
    parser.add_argument("archive", type=Path, help="ZIP archive of the official C# SDK at the pinned revision")
    parser.add_argument("--protoc", default="protoc", help="Path to the Protocol Buffers compiler")
    parser.add_argument("--check", action="store_true", help="Verify generated files instead of writing them")
    args = parser.parse_args()
    protoc = str(Path(args.protoc).resolve()) if Path(args.protoc).is_file() else args.protoc
    spec = json.loads(args.openapi.read_bytes())
    descriptor, revision = compile_descriptors(args.archive, protoc)
    outputs = {
        PACKAGE / "_diadoc.pb": descriptor,
        PACKAGE / "_methods.py": generate_methods(spec, descriptor, revision).encode("utf-8"),
    }
    for path, data in outputs.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                raise SystemExit(f"Generated file differs: {path.relative_to(ROOT)}")
        else:
            path.write_bytes(data)


if __name__ == "__main__":
    main()
