from typing import TYPE_CHECKING
from ubinascii import hexlify

from trezor import io, log, loop, ui
from trezor.enums import ButtonRequestType
from trezor.wire import ActionCancelled

import trezorui2

from ...components.common.confirm import CANCELLED, CONFIRMED, INFO
from ..common import button_request, interact

if TYPE_CHECKING:
    from typing import Any, Awaitable, Iterable, NoReturn, Sequence, TypeVar

    from trezor.wire import GenericContext, Context
    from ..common import PropertyType, ExceptionType

    T = TypeVar("T")


BR_TYPE_OTHER = ButtonRequestType.Other  # global_import_cache


class _RustLayout(ui.Layout):
    # pylint: disable=super-init-not-called
    def __init__(self, layout: Any, is_backup: bool = False):
        self.layout = layout
        self.timer = loop.Timer()
        self.layout.attach_timer_fn(self.set_timer)
        self.is_backup = is_backup

        if __debug__ and self.is_backup:
            self.notify_backup()

    def set_timer(self, token: int, deadline: int) -> None:
        self.timer.schedule(deadline, token)

    def request_complete_repaint(self) -> None:
        msg = self.layout.request_complete_repaint()
        assert msg is None

    if __debug__:

        def create_tasks(self) -> tuple[loop.AwaitableTask, ...]:
            from apps.debug import confirm_signal, input_signal

            return (
                self.handle_timers(),
                self.handle_input_and_rendering(),
                self.handle_swipe(),
                confirm_signal(),
                input_signal(),
            )

        def read_content(self) -> list[str]:
            result: list[str] = []

            def callback(*args: Any) -> None:
                for arg in args:
                    result.append(str(arg))

            self.layout.trace(callback)
            result = " ".join(result).split("\n")
            return result

        async def handle_swipe(self):
            from apps.debug import notify_layout_change, swipe_signal
            from trezor.ui.components.common import (
                SWIPE_UP,
                SWIPE_DOWN,
                SWIPE_LEFT,
                SWIPE_RIGHT,
            )

            while True:
                direction = await swipe_signal()
                orig_x = orig_y = 120
                off_x, off_y = {
                    SWIPE_UP: (0, -30),
                    SWIPE_DOWN: (0, 30),
                    SWIPE_LEFT: (-30, 0),
                    SWIPE_RIGHT: (30, 0),
                }[direction]

                for event, x, y in (
                    (io.TOUCH_START, orig_x, orig_y),
                    (io.TOUCH_MOVE, orig_x + 1 * off_x, orig_y + 1 * off_y),
                    (io.TOUCH_END, orig_x + 2 * off_x, orig_y + 2 * off_y),
                ):
                    msg = self.layout.touch_event(event, x, y)
                    self.layout.paint()
                    if msg is not None:
                        raise ui.Result(msg)

                if self.is_backup:
                    self.notify_backup()
                notify_layout_change(self)

        def notify_backup(self):
            from apps.debug import reset_current_words

            content = "\n".join(self.read_content())
            start = "< Paragraphs "
            end = ">"
            start_pos = content.index(start)
            end_pos = content.index(end, start_pos)
            words: list[str] = []
            for line in content[start_pos + len(start) : end_pos].split("\n"):
                line = line.strip()
                if not line:
                    continue
                space_pos = line.index(" ")
                words.append(line[space_pos + 1 :])
            reset_current_words.publish(words)

    else:

        def create_tasks(self) -> tuple[loop.AwaitableTask, ...]:
            return self.handle_timers(), self.handle_input_and_rendering()

    def _before_render(self) -> None:
        # Clear the screen of any leftovers.
        ui.backlight_fade(ui.style.BACKLIGHT_DIM)
        ui.display.clear()
        self.layout.paint()

        if __debug__ and self.should_notify_layout_change:
            from apps.debug import notify_layout_change

            # notify about change and do not notify again until next await.
            # (handle_rendering might be called multiple times in a single await,
            # because of the endless loop in __iter__)
            self.should_notify_layout_change = False
            notify_layout_change(self)

        # Turn the brightness on again.
        ui.backlight_fade(self.BACKLIGHT_LEVEL)

    def handle_input_and_rendering(self) -> loop.Task:  # type: ignore [awaitable-is-generator]
        from trezor import workflow

        touch = loop.wait(io.TOUCH)
        self._before_render()
        # self.layout.bounds()
        while True:
            # Using `yield` instead of `await` to avoid allocations.
            event, x, y = yield touch
            workflow.idle_timer.touch()
            msg = None
            if event in (io.TOUCH_START, io.TOUCH_MOVE, io.TOUCH_END):
                msg = self.layout.touch_event(event, x, y)
            if msg is not None:
                raise ui.Result(msg)
            self.layout.paint()
            # self.layout.bounds()

    def handle_timers(self) -> loop.Task:  # type: ignore [awaitable-is-generator]
        while True:
            # Using `yield` instead of `await` to avoid allocations.
            token = yield self.timer
            msg = self.layout.timer(token)
            if msg is not None:
                raise ui.Result(msg)
            self.layout.paint()

    def page_count(self) -> int:
        return self.layout.page_count()


async def raise_if_not_confirmed(a: Awaitable[T], exc: Any = ActionCancelled) -> T:
    result = await a
    if result is not CONFIRMED:
        raise exc
    return result


async def confirm_action(
    ctx: GenericContext,
    br_type: str,
    title: str,
    action: str | None = None,
    description: str | None = None,
    description_param: str | None = None,
    description_param_font: int = ui.BOLD,
    verb: str | bytes | None = "CONFIRM",
    verb_cancel: str | bytes | None = None,
    hold: bool = False,
    hold_danger: bool = False,
    reverse: bool = False,
    exc: ExceptionType = ActionCancelled,
    br_code: ButtonRequestType = BR_TYPE_OTHER,
) -> None:
    if isinstance(verb, bytes) or isinstance(verb_cancel, bytes):
        raise NotImplementedError
    if isinstance(verb, str):
        verb = verb.upper()
    if isinstance(verb_cancel, str):
        verb_cancel = verb_cancel.upper()

    if description is not None and description_param is not None:
        if description_param_font != ui.BOLD:
            log.error(__name__, "confirm_action description_param_font not implemented")
        description = description.format(description_param)

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_action(
                    title=title.upper(),
                    action=action,
                    description=description,
                    verb=verb,
                    verb_cancel=verb_cancel,
                    hold=hold,
                    hold_danger=hold_danger,
                    reverse=reverse,
                )
            ),
            br_type,
            br_code,
        ),
        exc,
    )


async def confirm_reset_device(
    ctx: GenericContext, prompt: str, recovery: bool = False
) -> None:
    if recovery:
        title = "RECOVERY MODE"
    else:
        title = "CREATE NEW WALLET"

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_reset_device(
                    title=title.upper(),
                    prompt=prompt.replace("\n", " "),
                )
            ),
            "recover_device" if recovery else "setup_device",
            ButtonRequestType.ProtectCall
            if recovery
            else ButtonRequestType.ResetDevice,
        )
    )


# TODO cleanup @ redesign
async def confirm_backup(ctx: GenericContext) -> bool:
    result = await interact(
        ctx,
        _RustLayout(
            trezorui2.confirm_action(
                title="SUCCESS",
                action="New wallet created successfully.",
                description="You should back up your new wallet right now.",
                verb="BACK UP",
                verb_cancel="SKIP",
            )
        ),
        "backup_device",
        ButtonRequestType.ResetDevice,
    )
    if result is CONFIRMED:
        return True

    result = await interact(
        ctx,
        _RustLayout(
            trezorui2.confirm_action(
                title="WARNING",
                action="Are you sure you want to skip the backup?",
                description="You can back up your Trezor once, at any time.",
                verb="BACK UP",
                verb_cancel="SKIP",
            )
        ),
        "backup_device",
        ButtonRequestType.ResetDevice,
    )
    return result is CONFIRMED


async def confirm_path_warning(
    ctx: GenericContext,
    path: str,
    path_type: str | None = None,
) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.show_warning(
                    title="Unknown path"
                    if not path_type
                    else f"Unknown {path_type.lower()}",
                    description=path,
                )
            ),
            "path_warning",
            ButtonRequestType.UnknownDerivationPath,
        )
    )


def _show_xpub(xpub: str, title: str, cancel: str) -> ui.Layout:
    content = _RustLayout(
        trezorui2.confirm_blob(
            title=title,
            data=xpub,
            verb_cancel=cancel,
        )
    )
    return content


async def show_xpub(ctx: GenericContext, xpub: str, title: str, cancel: str) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _show_xpub(xpub, title, cancel),
            "show_xpub",
            ButtonRequestType.PublicKey,
        )
    )


async def show_address(
    ctx: GenericContext,
    address: str,
    *,
    address_qr: str | None = None,
    case_sensitive: bool = True,
    title: str = "Confirm address",
    network: str | None = None,
    multisig_index: int | None = None,
    xpubs: Sequence[str] = (),
    address_extra: str | None = None,
    title_qr: str | None = None,
) -> None:
    is_multisig = len(xpubs) > 0
    while True:
        result = await interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_blob(
                    title=title.upper(),
                    data=address,
                    description=network or "",
                    extra=address_extra or "",
                    verb_cancel="QR",
                )
            ),
            "show_address",
            ButtonRequestType.Address,
        )
        if result is CONFIRMED:
            break

        result = await interact(
            ctx,
            _RustLayout(
                trezorui2.show_qr(
                    address=address if address_qr is None else address_qr,
                    case_sensitive=case_sensitive,
                    title=title.upper() if title_qr is None else title_qr.upper(),
                    verb_cancel="XPUBs" if is_multisig else "ADDRESS",
                )
            ),
            "show_qr",
            ButtonRequestType.Address,
        )
        if result is CONFIRMED:
            break

        if is_multisig:
            for i, xpub in enumerate(xpubs):
                cancel = "NEXT" if i < len(xpubs) - 1 else "ADDRESS"
                title_xpub = f"XPUB #{i + 1}"
                title_xpub += " (yours)" if i == multisig_index else " (cosigner)"
                result = await interact(
                    ctx,
                    _show_xpub(xpub, title=title_xpub, cancel=cancel),
                    "show_xpub",
                    ButtonRequestType.PublicKey,
                )
                if result is CONFIRMED:
                    return


def show_pubkey(
    ctx: Context, pubkey: str, title: str = "Confirm public key"
) -> Awaitable[None]:
    return confirm_blob(
        ctx,
        "show_pubkey",
        title,
        pubkey,
        br_code=ButtonRequestType.PublicKey,
    )


async def show_error_and_raise(
    ctx: GenericContext,
    br_type: str,
    content: str,
    subheader: str | None = None,
    button: str = "CLOSE",
    exc: ExceptionType = ActionCancelled,
) -> NoReturn:
    await interact(
        ctx,
        _RustLayout(
            trezorui2.show_error(
                title=content.replace("\n", " "),
                description=subheader or "",
                button=button.upper(),
                allow_cancel=False,
            )
        ),
        br_type,
        BR_TYPE_OTHER,
    )
    raise exc


async def show_warning(
    ctx: GenericContext,
    br_type: str,
    content: str,
    subheader: str | None = None,
    button: str = "TRY AGAIN",
    br_code: ButtonRequestType = ButtonRequestType.Warning,
) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.show_warning(
                    title=content.replace("\n", " "),
                    description=subheader or "",
                    button=button.upper(),
                    allow_cancel=False,
                )
            ),
            br_type,
            br_code,
        )
    )


async def show_success(
    ctx: GenericContext,
    br_type: str,
    content: str,
    subheader: str | None = None,
    button: str = "CONTINUE",
) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.show_success(
                    title=content.replace("\n", " "),
                    description=subheader or "",
                    button=button.upper(),
                    allow_cancel=False,
                )
            ),
            br_type,
            ButtonRequestType.Success,
        )
    )


async def confirm_output(
    ctx: GenericContext,
    address: str,
    amount: str,
    title: str = "SENDING",
    br_code: ButtonRequestType = ButtonRequestType.ConfirmOutput,
) -> None:
    title = title.upper()
    if title.startswith("CONFIRM "):
        title = title[len("CONFIRM ") :]

    await confirm_value(
        ctx,
        title,
        address,
        "To:",
        "confirm_output",
        br_code,
        verb="NEXT",
    )

    await confirm_value(
        ctx,
        title,
        amount,
        "Amount:",
        "confirm_output",
        br_code,
        verb="NEXT",
    )


async def confirm_payment_request(
    ctx: GenericContext,
    recipient_name: str,
    amount: str,
    memos: list[str],
) -> Any:
    from ...components.common import confirm

    result = await interact(
        ctx,
        _RustLayout(
            trezorui2.confirm_with_info(
                title="SENDING",
                items=[f"{amount} to\n{recipient_name}"] + memos,
                button="CONFIRM",
                info_button="DETAILS",
            )
        ),
        "confirm_payment_request",
        ButtonRequestType.ConfirmOutput,
    )
    if result is CONFIRMED:
        return confirm.CONFIRMED
    elif result is INFO:
        return confirm.INFO
    else:
        raise ActionCancelled


async def should_show_more(
    ctx: GenericContext,
    title: str,
    para: Iterable[tuple[int, str]],
    button_text: str = "Show all",
    br_type: str = "should_show_more",
    br_code: ButtonRequestType = BR_TYPE_OTHER,
    confirm: str | bytes | None = None,
) -> bool:
    """Return True if the user wants to show more (they click a special button)
    and False when the user wants to continue without showing details.

    Raises ActionCancelled if the user cancels.
    """
    if confirm is None or not isinstance(confirm, str):
        confirm = "CONFIRM"

    items = []
    for _font, text in para:
        items.append(text)

    result = await interact(
        ctx,
        _RustLayout(
            trezorui2.confirm_with_info(
                title=title.upper(),
                items=items,
                button=confirm.upper(),
                info_button=button_text.upper(),
            )
        ),
        br_type,
        br_code,
    )

    if result is CONFIRMED:
        return False
    elif result is INFO:
        return True
    else:
        assert result is CANCELLED
        raise ActionCancelled


async def confirm_blob(
    ctx: GenericContext,
    br_type: str,
    title: str,
    data: bytes | str,
    description: str | None = None,
    hold: bool = False,
    br_code: ButtonRequestType = BR_TYPE_OTHER,
    ask_pagination: bool = False,
) -> None:
    if isinstance(data, bytes):
        data = hexlify(data).decode()

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_blob(
                    title=title.upper(),
                    description=description or "",
                    data=data,
                    ask_pagination=ask_pagination,
                    hold=hold,
                )
            ),
            br_type,
            br_code,
        )
    )


async def confirm_address(
    ctx: GenericContext,
    title: str,
    address: str,
    description: str | None = "Address:",
    br_type: str = "confirm_address",
    br_code: ButtonRequestType = BR_TYPE_OTHER,
) -> None:
    return await confirm_value(
        ctx,
        title,
        address,
        description or "",
        br_type,
        br_code,
        verb="NEXT",
    )


async def confirm_text(
    ctx: GenericContext,
    br_type: str,
    title: str,
    data: str,
    description: str | None = None,
    br_code: ButtonRequestType = BR_TYPE_OTHER,
) -> None:
    return await confirm_value(
        ctx,
        title,
        data,
        description or "",
        br_type,
        br_code,
        verb="CONFIRM",
    )


def confirm_amount(
    ctx: GenericContext,
    title: str,
    amount: str,
    description: str = "Amount:",
    br_type: str = "confirm_amount",
    br_code: ButtonRequestType = BR_TYPE_OTHER,
) -> Awaitable[None]:
    return confirm_value(
        ctx,
        title,
        amount,
        description,
        br_type,
        br_code,
        verb="NEXT",
    )


def confirm_value(
    ctx: GenericContext,
    title: str,
    value: str,
    description: str,
    br_type: str,
    br_code: ButtonRequestType = ButtonRequestType.Other,
    *,
    verb: str | None = None,
    hold: bool = False,
) -> Awaitable[None]:
    """General confirmation dialog, used by many other confirm_* functions."""

    if not verb and not hold:
        raise ValueError("Either verb or hold=True must be set")

    return raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_value(
                    title=title.upper(),
                    description=description,
                    value=value,
                    verb=verb,
                    hold=hold,
                )
            ),
            br_type,
            br_code,
        )
    )


async def confirm_properties(
    ctx: GenericContext,
    br_type: str,
    title: str,
    props: Iterable[PropertyType],
    hold: bool = False,
    br_code: ButtonRequestType = ButtonRequestType.ConfirmOutput,
) -> None:
    def handle_bytes(prop: PropertyType) -> tuple[str | None, str | None, bool]:
        if isinstance(prop[1], bytes):
            return (prop[0], hexlify(prop[1]).decode(), True)
        else:
            return (prop[0], prop[1], False)

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_properties(
                    title=title.upper(),
                    items=map(handle_bytes, props),
                    hold=hold,
                )
            ),
            br_type,
            br_code,
        )
    )


async def confirm_total(
    ctx: GenericContext,
    total_amount: str,
    fee_amount: str,
    fee_rate_amount: str | None = None,
    title: str = "SENDING",
    total_label: str = "Total amount:",
    fee_label: str = "Fee:",
    br_type: str = "confirm_total",
    br_code: ButtonRequestType = ButtonRequestType.SignTx,
) -> None:
    await confirm_value(
        ctx,
        title,
        f"{fee_amount}\n({fee_rate_amount})"
        if fee_rate_amount is not None
        else fee_amount,
        fee_label,
        br_type,
        br_code,
        verb="NEXT",
    )

    await confirm_value(
        ctx,
        title,
        total_amount,
        total_label,
        br_type,
        br_code,
        hold=True,
    )


async def confirm_joint_total(
    ctx: GenericContext, spending_amount: str, total_amount: str
) -> None:

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_joint_total(
                    spending_amount=spending_amount,
                    total_amount=total_amount,
                )
            ),
            "confirm_joint_total",
            ButtonRequestType.SignTx,
        )
    )


async def confirm_metadata(
    ctx: GenericContext,
    br_type: str,
    title: str,
    content: str,
    param: str | None = None,
    br_code: ButtonRequestType = ButtonRequestType.SignTx,
    hold: bool = False,
) -> None:
    if param:
        content = content.format(param)

    if br_type == "fee_over_threshold":
        layout = trezorui2.show_warning(
            title="Unusually high fee",
            description=param or "",
        )
    elif br_type == "change_count_over_threshold":
        layout = trezorui2.show_warning(
            title="A lot of change-outputs",
            description=f"{param} outputs" if param is not None else "",
        )
    else:
        if param is not None:
            content = content.format(param)
        # TODO: "unverified external inputs"

        layout = trezorui2.confirm_action(
            title=title.upper(),
            action="",
            verb="NEXT",
            description=content,
            hold=hold,
        )

    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(layout),
            br_type,
            br_code,
        )
    )


async def confirm_replacement(ctx: GenericContext, description: str, txid: str) -> None:
    await confirm_blob(
        ctx,
        title=description.upper(),
        data=txid,
        description="Confirm transaction ID:",
        br_type="confirm_replacement",
        br_code=ButtonRequestType.SignTx,
    )


async def confirm_modify_output(
    ctx: GenericContext,
    address: str,
    sign: int,
    amount_change: str,
    amount_new: str,
) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_modify_output(
                    address=address,
                    sign=sign,
                    amount_change=amount_change,
                    amount_new=amount_new,
                )
            ),
            "modify_output",
            ButtonRequestType.ConfirmOutput,
        )
    )


async def confirm_modify_fee(
    ctx: GenericContext,
    sign: int,
    user_fee_change: str,
    total_fee_new: str,
    fee_rate_amount: str | None = None,
) -> None:
    # TODO: include fee_rate_amount
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_modify_fee(
                    sign=sign,
                    user_fee_change=user_fee_change,
                    total_fee_new=total_fee_new,
                )
            ),
            "modify_fee",
            ButtonRequestType.SignTx,
        )
    )


async def confirm_coinjoin(
    ctx: GenericContext, max_rounds: int, max_fee_per_vbyte: str
) -> None:
    await raise_if_not_confirmed(
        interact(
            ctx,
            _RustLayout(
                trezorui2.confirm_coinjoin(
                    max_rounds=str(max_rounds),
                    max_feerate=max_fee_per_vbyte,
                )
            ),
            "coinjoin_final",
            ButtonRequestType.Other,
        )
    )


def show_coinjoin() -> None:
    log.error(__name__, "show_coinjoin not implemented")


# TODO cleanup @ redesign
async def confirm_sign_identity(
    ctx: GenericContext, proto: str, identity: str, challenge_visual: str | None
) -> None:
    await confirm_blob(
        ctx,
        title=f"Sign {proto}",
        data=identity,
        description=challenge_visual + "\n" if challenge_visual else "",
        br_type="sign_identity",
        br_code=ButtonRequestType.Other,
    )


async def confirm_signverify(
    ctx: GenericContext, coin: str, message: str, address: str, verify: bool
) -> None:
    if verify:
        title = f"VERIFY {coin} MESSAGE"
        br_type = "verify_message"
    else:
        title = f"SIGN {coin} MESSAGE"
        br_type = "sign_message"

    await confirm_blob(
        ctx,
        br_type,
        title,
        address,
        "Confirm address:",
        br_code=ButtonRequestType.Other,
    )

    await confirm_blob(
        ctx,
        br_type,
        title,
        message,
        "Confirm message:",
        br_code=ButtonRequestType.Other,
    )


async def show_popup(
    title: str,
    description: str,
    subtitle: str | None = None,
    description_param: str = "",
    timeout_ms: int = 3000,
) -> None:
    if subtitle:
        title += f"\n{subtitle}"
    await _RustLayout(
        trezorui2.show_error(
            title=title,
            description=description.format(description_param),
            button="",
            time_ms=timeout_ms,
        )
    )


def draw_simple_text(title: str, description: str = "") -> None:
    log.error(__name__, "draw_simple_text not implemented")


async def request_passphrase_on_device(ctx: GenericContext, max_len: int) -> str:
    await button_request(
        ctx, "passphrase_device", code=ButtonRequestType.PassphraseEntry
    )

    keyboard = _RustLayout(
        trezorui2.request_passphrase(prompt="Enter passphrase", max_len=max_len)
    )
    result = await ctx.wait(keyboard)
    if result is CANCELLED:
        raise ActionCancelled("Passphrase entry cancelled")

    assert isinstance(result, str)
    return result


async def request_pin_on_device(
    ctx: GenericContext,
    prompt: str,
    attempts_remaining: int | None,
    allow_cancel: bool,
) -> str:
    from trezor.wire import PinCancelled

    await button_request(ctx, "pin_device", code=ButtonRequestType.PinEntry)

    warning = "Wrong PIN" if "Wrong" in prompt else None

    if attempts_remaining is None:
        subprompt = ""
    elif attempts_remaining == 1:
        prompt = "Enter PIN"
        subprompt = "Last attempt"
    else:
        prompt = "Enter PIN"
        subprompt = f"{attempts_remaining} tries left"

    dialog = _RustLayout(
        trezorui2.request_pin(
            prompt=prompt,
            subprompt=subprompt,
            allow_cancel=allow_cancel,
            warning=warning,
        )
    )
    while True:
        result = await ctx.wait(dialog)
        if result is CANCELLED:
            raise PinCancelled
        assert isinstance(result, str)
        return result
