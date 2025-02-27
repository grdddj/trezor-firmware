from typing import TYPE_CHECKING

from apps.common.keychain import auto_keychain

if TYPE_CHECKING:
    from trezor.messages import MoneroTransactionFinalAck
    from apps.common.keychain import Keychain
    from apps.monero.signing.state import State
    from trezor.wire import Context


@auto_keychain(__name__)
async def sign_tx(
    ctx: Context, received_msg, keychain: Keychain
) -> MoneroTransactionFinalAck:
    import gc
    from trezor import log, utils
    from apps.monero.signing.state import State

    state = State(ctx)
    mods = utils.unimport_begin()

    # Splitting ctx.call() to write() and read() helps to reduce memory fragmentation
    # between calls.
    while True:
        if __debug__:
            log.debug(__name__, "#### F: %s, A: %s", gc.mem_free(), gc.mem_alloc())
        gc.collect()
        gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())

        result_msg, accept_msgs = await _sign_tx_dispatch(state, received_msg, keychain)
        if accept_msgs is None:
            break

        await ctx.write(result_msg)
        del (result_msg, received_msg)
        utils.unimport_end(mods)

        received_msg = await ctx.read_any(accept_msgs)

    utils.unimport_end(mods)
    return result_msg


async def _sign_tx_dispatch(state: State, msg, keychain: Keychain) -> tuple:
    from trezor.enums import MessageType
    from trezor import wire

    MESSAGE_WIRE_TYPE = msg.MESSAGE_WIRE_TYPE  # local_cache_attribute

    if MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionInitRequest:
        from apps.monero.signing import step_01_init_transaction

        return (
            await step_01_init_transaction.init_transaction(
                state, msg.address_n, msg.network_type, msg.tsx_data, keychain
            ),
            (MessageType.MoneroTransactionSetInputRequest,),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionSetInputRequest:
        from apps.monero.signing import step_02_set_input

        return (
            await step_02_set_input.set_input(state, msg.src_entr),
            (
                MessageType.MoneroTransactionSetInputRequest,
                MessageType.MoneroTransactionInputViniRequest,
            ),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionInputViniRequest:
        from apps.monero.signing import step_04_input_vini

        return (
            await step_04_input_vini.input_vini(
                state, msg.src_entr, msg.vini, msg.vini_hmac, msg.orig_idx
            ),
            (
                MessageType.MoneroTransactionInputViniRequest,
                MessageType.MoneroTransactionAllInputsSetRequest,
            ),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionAllInputsSetRequest:
        from apps.monero.signing import step_05_all_inputs_set

        return (
            await step_05_all_inputs_set.all_inputs_set(state),
            (MessageType.MoneroTransactionSetOutputRequest,),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionSetOutputRequest:
        from apps.monero.signing import step_06_set_output

        is_offloaded_bp = bool(msg.is_offloaded_bp)
        dst, dst_hmac, rsig_data = msg.dst_entr, msg.dst_entr_hmac, msg.rsig_data
        del msg

        return (
            await step_06_set_output.set_output(
                state, dst, dst_hmac, rsig_data, is_offloaded_bp
            ),
            (
                MessageType.MoneroTransactionSetOutputRequest,
                MessageType.MoneroTransactionAllOutSetRequest,
            ),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionAllOutSetRequest:
        from apps.monero.signing import step_07_all_outputs_set

        return (
            await step_07_all_outputs_set.all_outputs_set(state),
            (MessageType.MoneroTransactionSignInputRequest,),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionSignInputRequest:
        from apps.monero.signing import step_09_sign_input

        return (
            await step_09_sign_input.sign_input(
                state,
                msg.src_entr,
                msg.vini,
                msg.vini_hmac,
                msg.pseudo_out,
                msg.pseudo_out_hmac,
                msg.pseudo_out_alpha,
                msg.spend_key,
                msg.orig_idx,
            ),
            (
                MessageType.MoneroTransactionSignInputRequest,
                MessageType.MoneroTransactionFinalRequest,
            ),
        )

    elif MESSAGE_WIRE_TYPE == MessageType.MoneroTransactionFinalRequest:
        from apps.monero.signing import step_10_sign_final

        return step_10_sign_final.final_msg(state), None

    else:
        raise wire.DataError("Unknown message")
