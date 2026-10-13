"""Formatting helpers for human-readable order codes."""


def format_order_code(order_id: int) -> str:
    """Return a positive integer order ID as a zero-padded order code."""
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ValueError("order_id must be a positive integer")
    return f"ORD-{order_id:06d}"
