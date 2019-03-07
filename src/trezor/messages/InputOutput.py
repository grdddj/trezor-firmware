# Automatically generated by pb2py
# fmt: off
import protobuf as p

from .Coin import Coin

if __debug__:
    try:
        from typing import List
    except ImportError:
        List = None  # type: ignore


class InputOutput(p.MessageType):

    def __init__(
        self,
        address: str = None,
        coins: List[Coin] = None,
    ) -> None:
        self.address = address
        self.coins = coins if coins is not None else []

    @classmethod
    def get_fields(cls):
        return {
            1: ('address', p.UnicodeType, 0),
            2: ('coins', Coin, p.FLAG_REPEATED),
        }
