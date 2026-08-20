"""Optional SDK providers; importing this module does not import any SDK."""

from .akshare import AKShareProvider
from .baostock import (
    BaoStockProvider,
    normalize_a_share_stock_instrument,
    normalize_baostock_instrument,
)
from .choice import ChoiceProvider
from .choice_index import ChoiceIndexProvider
from .csi_official import CSIOfficialProvider
from .eastmoney_legacy import EastmoneyLegacyProvider
from .sse_calendar import SSECalendarProvider
from .tushare import TushareProvider

__all__ = [
    "AKShareProvider",
    "BaoStockProvider",
    "ChoiceProvider",
    "ChoiceIndexProvider",
    "CSIOfficialProvider",
    "EastmoneyLegacyProvider",
    "SSECalendarProvider",
    "TushareProvider",
    "normalize_a_share_stock_instrument",
    "normalize_baostock_instrument",
]
