from micropython import const
from typing import TYPE_CHECKING

from trezor.crypto import hashlib
from trezor.enums import CardanoAddressType, CardanoGovernanceRegistrationFormat

from apps.common import cbor

from . import addresses, layout
from .helpers.paths import SCHEMA_STAKING_ANY_ACCOUNT
from .helpers.utils import derive_public_key

if TYPE_CHECKING:
    Delegations = list[tuple[bytes, int]]
    GovernanceRegistrationPayload = dict[int, Delegations | bytes | int]
    SignedGovernanceRegistrationPayload = tuple[GovernanceRegistrationPayload, bytes]
    GovernanceRegistrationSignature = dict[int, bytes]
    GovernanceRegistration = dict[
        int, GovernanceRegistrationPayload | GovernanceRegistrationSignature
    ]

    from trezor import messages
    from trezor.wire import Context

    from . import seed

_AUXILIARY_DATA_HASH_SIZE = const(32)
_GOVERNANCE_VOTING_PUBLIC_KEY_LENGTH = const(32)
_GOVERNANCE_REGISTRATION_HASH_SIZE = const(32)

_METADATA_KEY_GOVERNANCE_REGISTRATION = const(61284)
_METADATA_KEY_GOVERNANCE_REGISTRATION_SIGNATURE = const(61285)

_MAX_DELEGATION_COUNT = const(32)
_DEFAULT_VOTING_PURPOSE = const(0)


def assert_cond(condition: bool) -> None:
    from trezor import wire

    if not condition:
        raise wire.ProcessError("Invalid auxiliary data")


def validate(auxiliary_data: messages.CardanoTxAuxiliaryData) -> None:
    fields_provided = 0
    if auxiliary_data.hash:
        fields_provided += 1
        # _validate_hash
        assert_cond(len(auxiliary_data.hash) == _AUXILIARY_DATA_HASH_SIZE)
    if auxiliary_data.governance_registration_parameters:
        fields_provided += 1
        _validate_governance_registration_parameters(
            auxiliary_data.governance_registration_parameters
        )
    assert_cond(fields_provided == 1)


def _validate_governance_registration_parameters(
    parameters: messages.CardanoGovernanceRegistrationParametersType,
) -> None:
    voting_key_fields_provided = 0
    if parameters.voting_public_key is not None:
        voting_key_fields_provided += 1
        _validate_voting_public_key(parameters.voting_public_key)
    if parameters.delegations:
        voting_key_fields_provided += 1
        assert_cond(parameters.format == CardanoGovernanceRegistrationFormat.CIP36)
        _validate_delegations(parameters.delegations)
    assert_cond(voting_key_fields_provided == 1)

    assert_cond(SCHEMA_STAKING_ANY_ACCOUNT.match(parameters.staking_path))

    address_parameters = parameters.reward_address_parameters
    assert_cond(address_parameters.address_type != CardanoAddressType.BYRON)
    addresses.validate_address_parameters(address_parameters)

    if parameters.voting_purpose is not None:
        assert_cond(parameters.format == CardanoGovernanceRegistrationFormat.CIP36)


def _validate_voting_public_key(key: bytes) -> None:
    assert_cond(len(key) == _GOVERNANCE_VOTING_PUBLIC_KEY_LENGTH)


def _validate_delegations(
    delegations: list[messages.CardanoGovernanceDelegation],
) -> None:
    assert_cond(len(delegations) <= _MAX_DELEGATION_COUNT)
    for delegation in delegations:
        _validate_voting_public_key(delegation.voting_public_key)


def _get_voting_purpose_to_serialize(
    parameters: messages.CardanoGovernanceRegistrationParametersType,
) -> int | None:
    if parameters.format == CardanoGovernanceRegistrationFormat.CIP15:
        return None
    if parameters.voting_purpose is None:
        return _DEFAULT_VOTING_PURPOSE
    return parameters.voting_purpose


async def show(
    ctx: Context,
    keychain: seed.Keychain,
    auxiliary_data_hash: bytes,
    parameters: messages.CardanoGovernanceRegistrationParametersType | None,
    protocol_magic: int,
    network_id: int,
    should_show_details: bool,
) -> None:
    if parameters:
        await _show_governance_registration(
            ctx,
            keychain,
            parameters,
            protocol_magic,
            network_id,
            should_show_details,
        )

    if should_show_details:
        await layout.show_auxiliary_data_hash(ctx, auxiliary_data_hash)


async def _show_governance_registration(
    ctx: Context,
    keychain: seed.Keychain,
    parameters: messages.CardanoGovernanceRegistrationParametersType,
    protocol_magic: int,
    network_id: int,
    should_show_details: bool,
) -> None:
    from .helpers import bech32

    for delegation in parameters.delegations:
        encoded_public_key = bech32.encode(
            bech32.HRP_GOVERNANCE_PUBLIC_KEY, delegation.voting_public_key
        )
        await layout.confirm_governance_registration_delegation(
            ctx, encoded_public_key, delegation.weight
        )

    encoded_public_key: str | None = None
    if parameters.voting_public_key:
        encoded_public_key = bech32.encode(
            bech32.HRP_GOVERNANCE_PUBLIC_KEY, parameters.voting_public_key
        )

    reward_address = addresses.derive_human_readable(
        keychain,
        parameters.reward_address_parameters,
        protocol_magic,
        network_id,
    )

    voting_purpose: int | None = (
        _get_voting_purpose_to_serialize(parameters) if should_show_details else None
    )

    await layout.confirm_governance_registration(
        ctx,
        encoded_public_key,
        parameters.staking_path,
        reward_address,
        parameters.nonce,
        voting_purpose,
    )


def get_hash_and_supplement(
    keychain: seed.Keychain,
    auxiliary_data: messages.CardanoTxAuxiliaryData,
    protocol_magic: int,
    network_id: int,
) -> tuple[bytes, messages.CardanoTxAuxiliaryDataSupplement]:
    from trezor.enums import CardanoTxAuxiliaryDataSupplementType
    from trezor import messages

    if parameters := auxiliary_data.governance_registration_parameters:
        (
            governance_registration_payload,
            governance_signature,
        ) = _get_signed_governance_registration_payload(
            keychain, parameters, protocol_magic, network_id
        )
        auxiliary_data_hash = _get_governance_registration_hash(
            governance_registration_payload, governance_signature
        )
        auxiliary_data_supplement = messages.CardanoTxAuxiliaryDataSupplement(
            type=CardanoTxAuxiliaryDataSupplementType.GOVERNANCE_REGISTRATION_SIGNATURE,
            auxiliary_data_hash=auxiliary_data_hash,
            governance_signature=governance_signature,
        )
        return auxiliary_data_hash, auxiliary_data_supplement
    else:
        assert auxiliary_data.hash is not None  # validate_auxiliary_data
        return auxiliary_data.hash, messages.CardanoTxAuxiliaryDataSupplement(
            type=CardanoTxAuxiliaryDataSupplementType.NONE
        )


def _get_governance_registration_hash(
    governance_registration_payload: GovernanceRegistrationPayload,
    governance_registration_payload_signature: bytes,
) -> bytes:
    # _cborize_catalyst_registration
    governance_registration_signature = {1: governance_registration_payload_signature}
    cborized_catalyst_registration = {
        _METADATA_KEY_GOVERNANCE_REGISTRATION: governance_registration_payload,
        _METADATA_KEY_GOVERNANCE_REGISTRATION_SIGNATURE: governance_registration_signature,
    }

    # _get_hash
    # _wrap_metadata
    # A new structure of metadata is used after Cardano Mary era. The metadata
    # is wrapped in a tuple and auxiliary_scripts may follow it. Cardano
    # tooling uses this new format of "wrapped" metadata even if no
    # auxiliary_scripts are included. So we do the same here.
    # https://github.com/input-output-hk/cardano-ledger-specs/blob/f7deb22be14d31b535f56edc3ca542c548244c67/shelley-ma/shelley-ma-test/cddl-files/shelley-ma.cddl#L212
    metadata = (cborized_catalyst_registration, ())
    auxiliary_data = cbor.encode(metadata)
    return hashlib.blake2b(
        data=auxiliary_data, outlen=_AUXILIARY_DATA_HASH_SIZE
    ).digest()


def _get_signed_governance_registration_payload(
    keychain: seed.Keychain,
    parameters: messages.CardanoGovernanceRegistrationParametersType,
    protocol_magic: int,
    network_id: int,
) -> SignedGovernanceRegistrationPayload:
    delegations_or_key: Delegations | bytes
    if len(parameters.delegations) > 0:
        delegations_or_key = [
            (delegation.voting_public_key, delegation.weight)
            for delegation in parameters.delegations
        ]
    elif parameters.voting_public_key:
        delegations_or_key = parameters.voting_public_key
    else:
        raise RuntimeError  # should not be reached - _validate_governance_registration_parameters

    staking_key = derive_public_key(keychain, parameters.staking_path)

    reward_address = addresses.derive_bytes(
        keychain,
        parameters.reward_address_parameters,
        protocol_magic,
        network_id,
    )

    voting_purpose = _get_voting_purpose_to_serialize(parameters)

    payload: GovernanceRegistrationPayload = {
        1: delegations_or_key,
        2: staking_key,
        3: reward_address,
        4: parameters.nonce,
    }
    if voting_purpose is not None:
        payload[5] = voting_purpose

    signature = _create_governance_registration_payload_signature(
        keychain,
        payload,
        parameters.staking_path,
    )

    return payload, signature


def _create_governance_registration_payload_signature(
    keychain: seed.Keychain,
    governance_registration_payload: GovernanceRegistrationPayload,
    path: list[int],
) -> bytes:
    from trezor.crypto.curve import ed25519

    node = keychain.derive(path)

    encoded_governance_registration = cbor.encode(
        {_METADATA_KEY_GOVERNANCE_REGISTRATION: governance_registration_payload}
    )

    governance_registration_hash = hashlib.blake2b(
        data=encoded_governance_registration,
        outlen=_GOVERNANCE_REGISTRATION_HASH_SIZE,
    ).digest()

    return ed25519.sign_ext(
        node.private_key(), node.private_key_ext(), governance_registration_hash
    )
