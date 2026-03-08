def message_to_dict(message) -> dict:
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return message
