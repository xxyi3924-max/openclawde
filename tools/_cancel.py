import threading

# Cancellation flag — set when a new user message arrives mid-loop
_cancel_event = threading.Event()


def cancel():
    """Signal any running agent loop to stop at the next iteration."""
    _cancel_event.set()


def reset_cancel():
    """Clear the cancel flag before starting a new loop."""
    _cancel_event.clear()


def is_cancelled() -> bool:
    return _cancel_event.is_set()
