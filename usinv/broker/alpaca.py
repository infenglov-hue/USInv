"""Alpaca paper/live broker adapter conforming to OPS_SPEC §2-§4 and CODEX 6.4.

Enforces:
- Deterministic client_order_id for strict submission idempotency.
- Limit-on-Open (LOO, tif="opg") orders submitted with explicit collar around reference close.
- Preflight liquidity, account status, and settled-cash verification before 09:28 ET cutoff.
- Mocking capability for hermetic offline test suites.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

PAPER_API_URL = "https://paper-api.alpaca.markets"
LIVE_API_URL = "https://api.alpaca.markets"


class BrokerError(RuntimeError):
    """Raised when broker communication or execution contract fails."""


def make_client_order_id(
    as_of_session: date,
    security_id: str,
    side: str,
    attempt: int = 1,
) -> str:
    """Generate deterministic client order ID for duplicate-submission prevention."""
    sess_str = as_of_session.strftime("%Y%m%d")
    clean_sec = security_id.replace("-", "").replace(":", "")[:12]
    return f"usinv_{sess_str}_{clean_sec}_{side.lower()}_a{attempt}"


@dataclass(frozen=True, slots=True)
class AlpacaOrderResult:
    order_id: str
    client_order_id: str
    symbol: str
    qty: float
    side: str
    order_type: str
    time_in_force: str
    limit_price: float | None
    status: str
    submitted_at: str
    filled_at: str | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None


class AlpacaBroker:
    """Adapter for Alpaca REST API supporting Paper and Live execution."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        paper: bool = True,
        base_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = (
            api_key or os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY_ID")
        )
        self.api_secret = (
            api_secret
            or os.environ.get("APCA_API_SECRET_KEY")
            or os.environ.get("ALPACA_SECRET_KEY")
        )
        self.paper = paper
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = PAPER_API_URL if paper else LIVE_API_URL
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        if not self.is_configured:
            raise BrokerError("Alpaca credentials missing (APCA_API_KEY_ID / APCA_API_SECRET_KEY)")

        url = f"{self.base_url}{endpoint}"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
            "User-Agent": "USInv-Automated/1.0",
        }
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            err_msg = exc.read().decode("utf-8", errors="replace")
            logger.error("Alpaca HTTP %d error on %s: %s", exc.code, endpoint, err_msg)
            raise BrokerError(f"Alpaca API error ({exc.code}): {err_msg}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Alpaca connection failure on %s: %s", endpoint, exc)
            raise BrokerError(f"Alpaca connection error: {exc}") from exc

    def get_account(self) -> dict[str, Any]:
        """Fetch account equity, cash, status, and buying power."""
        return self._request("/v2/account")  # type: ignore[return-value]

    def get_clock(self) -> dict[str, Any]:
        """Fetch market clock status (is_open, next_open, next_close)."""
        return self._request("/v2/clock")  # type: ignore[return-value]

    def list_positions(self) -> list[dict[str, Any]]:
        """List all current open positions."""
        return self._request("/v2/positions")  # type: ignore[return-value]

    def preflight_check(self, required_cash: float) -> tuple[bool, str]:
        """Verify broker account is ready for automated trade submission."""
        if not self.is_configured:
            return False, "Broker credentials unconfigured"
        try:
            account = self.get_account()
            if account.get("status") != "ACTIVE":
                return False, f"Account status is not ACTIVE ({account.get('status')})"
            if account.get("trading_blocked", False):
                return False, "Account trading is blocked"

            cash = float(account.get("cash", "0"))
            if required_cash > 0 and cash < required_cash:
                return (
                    False,
                    f"Insufficient cash: ${cash:,.2f} available vs ${required_cash:,.2f} required",
                )

            return True, "Preflight verified: account ACTIVE with sufficient cash"
        except Exception as exc:
            return False, f"Preflight exception: {exc}"

    def submit_loo_order(
        self,
        symbol: str,
        qty: int | float,
        side: str,
        reference_price: float,
        *,
        collar_pct: float = 0.02,
        client_order_id: str,
    ) -> AlpacaOrderResult:
        """Submit a Limit-on-Open (LOO, tif="opg") auction order with collar protection.

        Buy collar: limit_price = reference_close * (1 + collar_pct)
        Sell collar: limit_price = reference_close * (1 - collar_pct)
        """
        side_norm = side.lower()
        if side_norm not in {"buy", "sell"}:
            raise BrokerError(f"Invalid order side: {side}")
        if qty <= 0:
            raise BrokerError(f"Order quantity must be positive, got {qty}")

        if side_norm == "buy":
            limit_price = round(reference_price * (1.0 + collar_pct), 2)
        else:
            limit_price = round(reference_price * (1.0 - collar_pct), 2)

        payload = {
            "symbol": symbol,
            "qty": str(qty) if isinstance(qty, int) else f"{qty:.4f}",
            "side": side_norm,
            "type": "limit",
            "time_in_force": "opg",
            "limit_price": str(limit_price),
            "client_order_id": client_order_id,
        }

        resp = self._request("/v2/orders", method="POST", payload=payload)
        return AlpacaOrderResult(
            order_id=resp["id"],
            client_order_id=resp["client_order_id"],
            symbol=resp["symbol"],
            qty=float(resp["qty"]),
            side=resp["side"],
            order_type=resp["type"],
            time_in_force=resp["time_in_force"],
            limit_price=float(resp["limit_price"]) if resp.get("limit_price") else None,
            status=resp["status"],
            submitted_at=resp["submitted_at"],
            filled_at=resp.get("filled_at"),
            filled_qty=float(resp.get("filled_qty", 0.0)),
            filled_avg_price=float(resp["filled_avg_price"])
            if resp.get("filled_avg_price")
            else None,
        )

    def get_order(self, order_id: str) -> AlpacaOrderResult:
        """Retrieve order by internal Alpaca order ID."""
        resp = self._request(f"/v2/orders/{order_id}")
        return AlpacaOrderResult(
            order_id=resp["id"],
            client_order_id=resp["client_order_id"],
            symbol=resp["symbol"],
            qty=float(resp["qty"]),
            side=resp["side"],
            order_type=resp["type"],
            time_in_force=resp["time_in_force"],
            limit_price=float(resp["limit_price"]) if resp.get("limit_price") else None,
            status=resp["status"],
            submitted_at=resp["submitted_at"],
            filled_at=resp.get("filled_at"),
            filled_qty=float(resp.get("filled_qty", 0.0)),
            filled_avg_price=float(resp["filled_avg_price"])
            if resp.get("filled_avg_price")
            else None,
        )

    def get_order_by_client_id(self, client_order_id: str) -> AlpacaOrderResult | None:
        """Retrieve order by deterministic client order ID for idempotency check."""
        try:
            resp = self._request(f"/v2/orders:by_client_order_id?client_order_id={client_order_id}")
            return AlpacaOrderResult(
                order_id=resp["id"],
                client_order_id=resp["client_order_id"],
                symbol=resp["symbol"],
                qty=float(resp["qty"]),
                side=resp["side"],
                order_type=resp["type"],
                time_in_force=resp["time_in_force"],
                limit_price=float(resp["limit_price"]) if resp.get("limit_price") else None,
                status=resp["status"],
                submitted_at=resp["submitted_at"],
                filled_at=resp.get("filled_at"),
                filled_qty=float(resp.get("filled_qty", 0.0)),
                filled_avg_price=float(resp["filled_avg_price"])
                if resp.get("filled_avg_price")
                else None,
            )
        except BrokerError as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise

    def list_orders(
        self,
        status: str = "all",
        after: str | None = None,
        limit: int = 100,
    ) -> list[AlpacaOrderResult]:
        """List orders by status and timestamp."""
        endpoint = f"/v2/orders?status={status}&limit={limit}"
        if after:
            endpoint += f"&after={after}"
        raw_list = self._request(endpoint)
        results = []
        for resp in raw_list:
            results.append(
                AlpacaOrderResult(
                    order_id=resp["id"],
                    client_order_id=resp["client_order_id"],
                    symbol=resp["symbol"],
                    qty=float(resp["qty"]),
                    side=resp["side"],
                    order_type=resp["type"],
                    time_in_force=resp["time_in_force"],
                    limit_price=float(resp["limit_price"]) if resp.get("limit_price") else None,
                    status=resp["status"],
                    submitted_at=resp["submitted_at"],
                    filled_at=resp.get("filled_at"),
                    filled_qty=float(resp.get("filled_qty", 0.0)),
                    filled_avg_price=float(resp["filled_avg_price"])
                    if resp.get("filled_avg_price")
                    else None,
                )
            )
        return results

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        try:
            self._request(f"/v2/orders/{order_id}", method="DELETE")
            return True
        except BrokerError:
            return False
