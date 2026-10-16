"""Facts about Audiobookshelf library items that several feeds must agree on."""


def has_ebook(item):
    """True if an ABS library item (or a collection/series book) has an ebook file.

    The single definition every feed uses. It used to be decided five ways
    (``ebookFormat`` only, ``ebookFile`` only, either), so a book could be
    counted on one page and missing from another. Minified item payloads carry
    only ``media.ebookFormat``; expanded ones carry ``media.ebookFile``.
    """
    media = item.get("media") or {}
    return media.get("ebookFile") is not None or bool(media.get("ebookFormat"))
