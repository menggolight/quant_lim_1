"""Read-only broker fact adapters.

Broker snapshots deliberately live outside the strategy ledger.  A broker
account may contain long-term holdings that the automated strategy does not
own.
"""

from trading.brokers.htsc_mquant_shadow import (
    HtscMQuantShadowAdapter,
    SnapshotValidationError,
)
from trading.brokers.models import (
    BrokerFunds,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    RawBrokerSnapshot,
)

__all__ = [
    "BrokerFunds",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerTrade",
    "HtscMQuantShadowAdapter",
    "RawBrokerSnapshot",
    "SnapshotValidationError",
]
