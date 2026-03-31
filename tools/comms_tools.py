def send_message(message: str, send_update=None) -> str:
    if send_update:
        send_update(message)
    return "Update sent"
