"""Shared helpers for the current chat-photo cache."""


def photo_id(entity):
    value = getattr(getattr(entity, "photo", None), "photo_id", None)
    return str(value) if value else ""


def cached_file(data_dir, relative):
    if not relative:
        return None
    root = (data_dir / "avatars").resolve()
    target = (root / relative).resolve()
    return target if target.is_relative_to(root) and target.is_file() else None
