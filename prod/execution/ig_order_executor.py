# prod/execution/ig_order_executor.py
"""
IG OTC order execution layer.
Replaces mt5 order_executor for IG-mode execution.

Paper mode:
  - Simulates fills. Zero REST calls. Returns synthetic success result.

Live mode:
  - Calls IG REST via trading_ig IGService.
  - Polls /confirms endpoint to get final deal_id and fill price.
  - Retry logic for transient errors (REQUEST_TIMEOUT, EXCHANGE_MANUAL_OVERRIDE, etc.)

Result dict contract (same shape as mt5 executor for orchestrator compatibility):
    success   : bool
    deal_id   : str   (IG dealId — stored where mt5_ticket was)
    deal_ref  : str   (IG dealReference)
    price     : float (fill price)
    volume    : float (filled size)
    status    : str   (OPEN | CLOSED | DELETED | AMENDED | UNKNOWN)
    reason    : str   (IG rejection reason or "ok")
    request   : dict  (original request for audit)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("ig_order_executor")

# IG deal status values
_STATUS_SUCCESS = {"OPEN", "AFFECTED", "AMENDED"}
_STATUS_CLOSED = {"CLOSED", "DELETED"}

# IG rejection reasons that warrant a retry
_RETRY_REASONS = {
    "EXCHANGE_MANUAL_OVERRIDE",
    "REQUEST_TIMEOUT",
    "SYSTEM_BUSY",
}


def send_order_ig(
    request: Dict[str, Any],
    ig_service: Any,
    environment: str = "paper",
    max_retries: int = 3,
    retry_delay: float = 2.0,
    confirm_poll_attempts: int = 5,
    confirm_poll_delay: float = 1.0,
) -> Dict[str, Any]:
    """
    Send an order to IG Group (open or close position).

    Args:
        request       : Dict from ig_order_builder (build_entry_request_ig or build_close_request_ig)
        ig_service    : IGService instance (connected, from IGConnector.service)
        environment   : "paper" | "live"
        max_retries   : Retry attempts for transient failures
        retry_delay   : Seconds between retries
        confirm_poll_attempts : Times to poll /confirms before giving up
        confirm_poll_delay    : Seconds between confirm polls

    Returns:
        Result dict (see module docstring)
    """
    # ── Paper mode ─────────────────────────────────────────────────────────────
    if environment == "paper":
        logger.info(
            f"[PAPER] IG ORDER SIMULATED — epic={request.get('epic')} "
            f"size={request.get('size')} direction={request.get('direction')} "
            f"stop_level={request.get('stop_level')}"
        )
        return {
            "success": True,
            "deal_id": "PAPER_DEAL_000",
            "deal_ref": "PAPER_REF_000",
            "price": 0.0,
            "volume": float(request.get("size", 0.0)),
            "status": "OPEN",
            "reason": "paper_trade",
            "request": request,
        }

    # ── Live mode ──────────────────────────────────────────────────────────────
    if ig_service is None:
        raise RuntimeError("ig_service is None — IGConnector not connected.")

    is_close = "deal_id" in request   # Close requests have deal_id key
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            if is_close:
                response = _send_close(ig_service, request)
            else:
                response = _send_open(ig_service, request)
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                f"IG order attempt {attempt}/{max_retries} raised: {exc}"
            )
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            break

        deal_ref = response.get("dealReference", "")
        logger.debug(f"IG order sent — dealReference={deal_ref}")

        if not deal_ref:
            last_error = "Empty dealReference returned"
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            break

        # Poll confirm endpoint
        confirm = _poll_confirm(
            ig_service, deal_ref,
            confirm_poll_attempts, confirm_poll_delay,
        )

        if confirm is None:
            last_error = f"Confirm polling timed out for dealRef={deal_ref}"
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            break

        status = confirm.get("dealStatus", "UNKNOWN")
        reason = confirm.get("reason", "UNKNOWN")

        if status in _STATUS_SUCCESS:
            fill_price = float(confirm.get("level", 0.0) or 0.0)
            filled_size = float(confirm.get("size", request.get("size", 0.0)) or 0.0)
            deal_id = confirm.get("dealId", "")

            logger.info(
                f"IG ORDER FILLED — epic={request.get('epic')} "
                f"dealId={deal_id} price={fill_price} size={filled_size} "
                f"status={status}"
            )
            return {
                "success": True,
                "deal_id": deal_id,
                "deal_ref": deal_ref,
                "price": fill_price,
                "volume": filled_size,
                "status": status,
                "reason": reason,
                "request": request,
            }

        # Retriable rejection
        if reason in _RETRY_REASONS and attempt < max_retries:
            logger.warning(
                f"IG order rejected reason={reason} — "
                f"attempt {attempt}/{max_retries}, retrying..."
            )
            last_error = reason
            time.sleep(retry_delay)
            continue

        # Fatal rejection
        logger.error(
            f"IG ORDER REJECTED — epic={request.get('epic')} "
            f"status={status} reason={reason}"
        )
        return {
            "success": False,
            "deal_id": "",
            "deal_ref": deal_ref,
            "price": 0.0,
            "volume": 0.0,
            "status": status,
            "reason": reason,
            "request": request,
        }

    # Max retries exhausted
    logger.error(
        f"IG ORDER FAILED after {max_retries} attempts — "
        f"epic={request.get('epic')} last_error={last_error}"
    )
    return {
        "success": False,
        "deal_id": "",
        "deal_ref": "",
        "price": 0.0,
        "volume": 0.0,
        "status": "FAILED",
        "reason": last_error or "max_retries_exceeded",
        "request": request,
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _send_open(ig_service: Any, req: Dict[str, Any]) -> Dict[str, Any]:
    """Call IGService.create_open_position — strip private keys first."""
    params = {k: v for k, v in req.items() if not k.startswith("_")}
    result = ig_service.create_open_position(**params)
    if isinstance(result, dict):
        return result
    # Some library versions return a Response object
    return result.__dict__ if hasattr(result, "__dict__") else {}


def _send_close(ig_service: Any, req: Dict[str, Any]) -> Dict[str, Any]:
    """Call IGService.close_open_position — strip private keys first."""
    params = {k: v for k, v in req.items() if not k.startswith("_")}
    result = ig_service.close_open_position(**params)
    if isinstance(result, dict):
        return result
    return result.__dict__ if hasattr(result, "__dict__") else {}


def _poll_confirm(
    ig_service: Any,
    deal_ref: str,
    attempts: int,
    delay: float,
) -> Optional[Dict[str, Any]]:
    """
    Poll /confirms/{deal_ref} until a terminal status is reached.
    Returns confirm dict or None on timeout.
    """
    for i in range(attempts):
        try:
            confirm = ig_service.fetch_deal_by_deal_reference(deal_ref)
            if isinstance(confirm, dict):
                status = confirm.get("dealStatus", "")
                if status and status not in ("PENDING", ""):
                    return confirm
        except Exception as exc:
            logger.debug(f"Confirm poll attempt {i+1}: {exc}")
        if i < attempts - 1:
            time.sleep(delay)
    return None
