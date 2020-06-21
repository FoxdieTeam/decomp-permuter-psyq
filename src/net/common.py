from dataclasses import dataclass
import json
from socket import socket
import struct
import sys
import toml
from typing import Optional, Type, TypeVar, Union

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey, VerifyKey
from nacl.public import Box, PrivateKey, PublicKey, SealedBox

T = TypeVar("T")

PROTOCOL_VERSION = 1

CONFIG_FILENAME = "pah.conf"


@dataclass
class RemoteServer:
    ip: str
    port: int
    ver_key: VerifyKey


@dataclass
class RawConfig:
    auth_server: Optional[str] = None
    auth_verify_key: Optional[VerifyKey] = None
    signing_key: Optional[SigningKey] = None
    initial_setup_nickname: Optional[str] = None


@dataclass
class Config:
    auth_server: str
    auth_verify_key: VerifyKey
    signing_key: SigningKey


def read_config() -> RawConfig:
    config = RawConfig()
    try:
        with open(CONFIG_FILENAME) as f:
            obj = toml.load(f)

        def read(key: str, t: Type[T]) -> Optional[T]:
            ret = obj.get(key)
            return ret if isinstance(ret, t) else None

        temp = read("auth_public_key", str)
        if temp:
            config.auth_verify_key = VerifyKey(HexEncoder.decode(temp))
        temp = read("secret_key", str)
        if temp:
            config.signing_key = SigningKey(HexEncoder.decode(temp))
        config.initial_setup_nickname = read("initial_setup_nickname", str)
        config.auth_server = read("auth_server", str)
    except FileNotFoundError:
        pass
    except Exception:
        print(f"Malformed configuration file {CONFIG_FILENAME}.\n")
        raise
    return config


def write_config(config: RawConfig) -> None:
    obj = {}

    def write(key: str, val: Union[None, str, int]) -> None:
        if val is not None:
            obj[key] = val

    write("initial_setup_nickname", config.initial_setup_nickname)
    write("auth_server", config.auth_server)

    key_hex: bytes
    if config.auth_verify_key:
        key_hex = config.auth_verify_key.encode(HexEncoder)
        write("auth_public_key", key_hex.decode("utf-8"))
    if config.signing_key:
        key_hex = config.signing_key.encode(HexEncoder)
        write("secret_key", key_hex.decode("utf-8"))

    with open(CONFIG_FILENAME, "w") as f:
        toml.dump(obj, f)


def socket_read_fixed(sock: socket, n: int) -> bytes:
    ret = []
    while n > 0:
        data = sock.recv(min(n, 4096))
        if not data:
            raise Exception("eof")
        ret.append(data)
        n -= len(data)
    return b"".join(ret)


def json_prop(obj: dict, prop: str, t: Type[T]) -> T:
    ret = obj.get(prop)
    if not isinstance(ret, t):
        found_type = type(ret).__name__
        raise ValueError(f"Member {prop} must have type {t.__name__}; got {found_type}")
    return ret


class Port:
    def __init__(self, sock: socket, box: Box, *, is_client: bool) -> None:
        self._sock = sock
        self._box = box
        self._send_nonce = 0 if is_client else 1
        self._receive_nonce = 1 if is_client else 0

    def send(self, msg: bytes) -> None:
        nonce = struct.pack(">24xQ", self._send_nonce)
        self._send_nonce += 2
        data = self._box.encrypt(msg, nonce)
        length_data = struct.pack(">Q", len(data))
        self._sock.sendall(length_data + data)

    def send_json(self, msg: dict) -> None:
        self.send(json.dumps(msg).encode("utf-8"))

    def receive(self) -> bytes:
        length_data = socket_read_fixed(self._sock, 8)
        length = struct.unpack(">Q", length_data)[0]
        data = socket_read_fixed(self._sock, length)
        nonce = struct.pack(">24xQ", self._receive_nonce)
        self._receive_nonce += 2
        ret: bytes = self._box.decrypt(data, nonce)
        return ret

    def receive_json(self) -> dict:
        ret = json.loads(self.receive())
        if not isinstance(ret, dict):
            # We always pass dictionaries as messages and no other data types,
            # to ensure future extensibility. (Other types are rare in
            # practice, anyway.)
            raise ValueError("Top-level JSON value must be a dictionary")
        return ret
