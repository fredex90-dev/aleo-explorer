from io import BytesIO
from typing import Optional, cast

import aleo_explorer_rust

from aleo_types import Address, Field, StructPlaintext, Vec, Tuple, Identifier, Plaintext, u8, LiteralType, Value, \
    PlaintextValue, LiteralPlaintext, Literal, ArrayPlaintext, Scalar, u32, u128
from aleo_types.cached import cached_get_key_id, cached_get_mapping_id
from db import Database
from node import Network
from util.aleo_strings import string_from_u128_list_le, string_to_u128_array_le, string_from_u128_array_le
from util.global_cache import global_mapping_cache


async def _get_mapping_value(db: Database, program_id: str, mapping_name: str, key: Plaintext) -> Optional[Plaintext]:
    mapping_id = Field.loads(cached_get_mapping_id(program_id, mapping_name))
    key_id = Field.loads(cached_get_key_id(program_id, mapping_name, key.dump()))
    if mapping_id in global_mapping_cache:
        if key_id not in global_mapping_cache[mapping_id]:
            return None
        return global_mapping_cache[mapping_id][key_id]["value"]
    data = await db.get_mapping_value(program_id, mapping_name, str(key_id))
    if data is None:
        return None
    value = Value.load(BytesIO(data))
    if not isinstance(value, PlaintextValue):
        raise RuntimeError(f"mapping value is not a plaintext: {value}")
    return value.plaintext


def _get_name_hash(name_st: StructPlaintext) -> Field:
    name_field = Field.load(BytesIO(
        aleo_explorer_rust.hash_ops(PlaintextValue(plaintext=name_st).dump(), "psd2", LiteralType.Field)
    ))
    zero_field_plaintext = LiteralPlaintext(literal=Literal(type_=Literal.Type.Field, primitive=Field(data=0)))
    data_struct = PlaintextValue(
        plaintext=StructPlaintext(
            members=Vec[Tuple[Identifier, Plaintext], u8]([
                Tuple[Identifier, Plaintext]((
                    Identifier(value="metadata"),
                    ArrayPlaintext(
                        elements=Vec[Plaintext, u32]([
                            LiteralPlaintext(
                                literal=Literal(type_=Literal.Type.Field, primitive=name_field)
                            ),
                            zero_field_plaintext,
                            zero_field_plaintext,
                            zero_field_plaintext,
                        ])
                    )
                ))
            ])
        )
    )
    data_hash = Field.load(BytesIO(
        aleo_explorer_rust.hash_ops(data_struct.dump(), "bhp256", LiteralType.Field)
    ))
    data_hash_value = PlaintextValue(
        plaintext=LiteralPlaintext(literal=Literal(type_=Literal.Type.Field, primitive=data_hash)))
    return Field.load(BytesIO(
        aleo_explorer_rust.commit_ops(data_hash_value.dump(), Scalar(0), "bhp256", LiteralType.Field)
    ))


def _get_name_st(name: str, parent: Field) -> StructPlaintext:
    return StructPlaintext(
        members=Vec[Tuple[Identifier, Plaintext], u8]([
            Tuple[Identifier, Plaintext]((
                Identifier(value="name"),
                string_to_u128_array_le(name, 4)
            )),
            Tuple[Identifier, Plaintext]((
                Identifier(value="parent"),
                LiteralPlaintext(literal=Literal(type_=Literal.Type.Field, primitive=parent))
            ))
        ])
    )


async def _resolve_name_hash(db: Database, name_hash: Field) -> Optional[str]:
    key = LiteralPlaintext(literal=Literal(type_=Literal.Type.Field, primitive=name_hash))
    name_struct = await _get_mapping_value(db, Network.ans_registry, "names", key)
    if name_struct is None:
        return None

    if not isinstance(name_struct, StructPlaintext):
        raise RuntimeError(f"mapping value is not a struct: {name_struct}")
    name = name_struct["name"]
    if not isinstance(name, ArrayPlaintext):
        raise RuntimeError(f"mapping value is not an array: {name}")
    name_str = string_from_u128_array_le(name)
    parent = name_struct["parent"]
    if not isinstance(parent, LiteralPlaintext):
        raise RuntimeError(f"mapping value is not a literal: {parent}")
    if not isinstance(parent.literal.primitive, Field):
        raise RuntimeError(f"mapping value is not a field: {parent.literal.primitive}")
    if parent.literal.primitive != Field(data=0):
        parent_name = await _resolve_name_hash(db, parent.literal.primitive)
        if parent_name is None:
            return None
        return f"{name_str}.{parent_name}"
    return name_str


async def get_address_from_domain(db: Database, domain: str) -> Optional[str]:
    domain_parts = domain.split(".")
    parent_hash = Field(data=0)
    for part in reversed(domain_parts):
        name_st = _get_name_st(part, parent_hash)
        parent_hash = _get_name_hash(name_st)
    name_hash = LiteralPlaintext(literal=Literal(type_=Literal.Type.Field, primitive=parent_hash))

    # name exists?
    name_struct = await _get_mapping_value(db, Network.ans_registry, "names", name_hash)
    if name_struct is None:
        return None
    if not isinstance(name_struct, StructPlaintext):
        raise RuntimeError(f"mapping value is not a struct: {name_struct}")

    # public owner?
    owner = await _get_mapping_value(db, Network.ans_registry, "nft_owners", name_hash)
    if owner is None:
        return ""
    if not isinstance(owner, LiteralPlaintext):
        raise RuntimeError(f"mapping value is not a literal: {owner}")
    if owner.literal.type != Literal.Type.Address:
        raise RuntimeError(f"mapping value is not an address: {owner.literal}")
    return str(owner.literal.primitive)

    # resolver = name_struct["resolver"]
    # if not isinstance(resolver, LiteralPlaintext):
    #     return None
    # if resolver.literal.primitive == u128():
    #     resolver_address = Testnet3.ans_registry


async def get_primary_name_from_address(db: Database, address: str) -> Optional[str]:
    key = LiteralPlaintext(literal=Literal(type_=Literal.Type.Address, primitive=Address.loads(address)))
    name_hash = await _get_mapping_value(db, Network.ans_registry, "primary_names", key)
    if name_hash is None:
        return None
    if not isinstance(name_hash, LiteralPlaintext):
        raise RuntimeError(f"mapping value is not a literal: {name_hash}")
    if not isinstance(name_hash.literal.primitive, Field):
        raise RuntimeError(f"mapping value is not a field: {name_hash.literal}")
    return await _resolve_name_hash(db, name_hash.literal.primitive)


async def get_all_names(db: Database) -> list[str]:
    mapping_id = Field.loads(cached_get_mapping_id(Network.ans_registry, "names"))
    if mapping_id not in global_mapping_cache:
        mapping = await db.get_mapping_cache(Network.ans_registry, "names")
    else:
        mapping = global_mapping_cache[mapping_id]
    values: list[PlaintextValue] = [x["value"] for x in mapping.values()]
    plaintexts = cast(list[StructPlaintext], [x.plaintext for x in values])
    # TODO: add session-persistent name hash cache in the future when there are too many names
    name_hash_cache: dict[Field, tuple[str, Field]] = {}
    token_values: list[tuple[str, Field]] = []
    for st in plaintexts:
        name_array = cast(ArrayPlaintext, st["name"])
        data: list[u128] = [
            cast(u128, cast(LiteralPlaintext, x).literal.primitive) for x in name_array
        ]
        name = string_from_u128_list_le(data)
        token_values.append((name, cast(Field, cast(LiteralPlaintext, st["parent"]).literal.primitive)))
        name_st = StructPlaintext(
            members=Vec[Tuple[Identifier, Plaintext], u8]([
                Tuple[Identifier, Plaintext]((Identifier(value="name"), name_array)),
                Tuple[Identifier, Plaintext]((Identifier(value="parent"), cast(LiteralPlaintext, st["parent"]))),
            ])
        )
        name_hash_cache[_get_name_hash(name_st)] = (name, cast(Field, cast(LiteralPlaintext, st["parent"]).literal.primitive))
    names: list[str] = []
    def resolve_name(partial_name: str, _parent_hash: Field) -> str:
        if _parent_hash == Field(0):
            return partial_name
        next_name, next_parent_hash = name_hash_cache[_parent_hash]
        return resolve_name(f"{partial_name}.{next_name}", next_parent_hash)
    for name, parent_hash in token_values:
        names.append(resolve_name(name, parent_hash))
    return names



