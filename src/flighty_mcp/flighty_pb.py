"""Load the Flighty protobuf schema and encode/decode messages.

    import flighty_pb as fp
    msg = fp.decode("SearchResponse", data)
    out = fp.encode(msg)

On first import the .proto in ./schema is compiled into ./_gen (needs
`protobuf` and `grpcio-tools`).
"""
from __future__ import annotations

import datetime
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, "schema", "flighty.proto")
GEN = os.path.join(HERE, "_gen")


def _compile() -> None:
    import grpc_tools
    from grpc_tools import protoc

    os.makedirs(GEN, exist_ok=True)
    wkt = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")
    rc = protoc.main(["protoc", f"-I{os.path.dirname(PROTO)}", f"-I{wkt}",
                      f"--python_out={GEN}", PROTO])
    if rc != 0:
        raise RuntimeError("protoc failed; see output above")


_out = os.path.join(GEN, "flighty_pb2.py")
if not os.path.exists(_out) or os.path.getmtime(_out) < os.path.getmtime(PROTO):
    _compile()
sys.path.insert(0, GEN)
pb = importlib.import_module("flighty_pb2")


def message_class(name: str):
    return getattr(pb, name)


def decode(name: str, data: bytes):
    msg = message_class(name)()
    msg.ParseFromString(data)
    return msg


def encode(msg) -> bytes:
    return msg.SerializeToString()


def ts(dt: datetime.datetime | str):
    """Build a google.protobuf.Timestamp from a datetime or ISO-8601 string."""
    from google.protobuf.timestamp_pb2 import Timestamp

    if isinstance(dt, str):
        dt = datetime.datetime.fromisoformat(dt.replace("Z", "+00:00"))
    t = Timestamp()
    t.FromDatetime(dt.astimezone(datetime.timezone.utc).replace(tzinfo=None))
    return t
