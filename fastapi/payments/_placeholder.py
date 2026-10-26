"""
Shared base for gateways whose integration isn't built yet.

They implement the full Provider interface so the registry, admin panel and
webhook router treat them like any other provider: `is_configured()` reflects
whether their env secrets exist, but `implemented = False` keeps them out of
active selection and failover, and every operation raises
ProviderNotImplemented (→ HTTP 503 provider_unavailable at checkout/portal,
501 provider_not_implemented at the webhook).

To build one: implement the three operations in its module, set
`implemented = True`, add tests, then enable it in Admin → Payments.
"""

from __future__ import annotations

from typing import Mapping, Optional

from payments.base import (
    BillingUser,
    CheckoutResult,
    Membership,
    NormalizedEvent,
    Provider,
    ProviderNotImplemented,
)


class PlaceholderProvider(Provider):
    implemented = False

    async def create_checkout(
        self,
        user: BillingUser,
        tier: str,
        platform: str,
        *,
        success_url: str,
        cancel_url: str,
        prefer: Optional[str] = None,
    ) -> CheckoutResult:
        raise ProviderNotImplemented(f"{self.id}: create_checkout not implemented")

    async def create_portal(self, user: BillingUser, membership: Membership, *, return_url: str) -> str:
        raise ProviderNotImplemented(f"{self.id}: create_portal not implemented")

    async def verify_and_parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Optional[NormalizedEvent]:
        raise ProviderNotImplemented(f"{self.id}: webhooks not implemented")
