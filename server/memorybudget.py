"""Charge JSON structure as well as wire length before allocating its object graph.

These are conservative retained-object/parse-workspace byte bounds, not an RSS
measurement. Container tables, scalar objects and Unicode width vary independently
of request wire length; a fixed body multiplier alone cannot bound them.
"""
from __future__ import annotations

import sys


def json_allocation_bound(chunks) -> int:
    """Scan bytes without copying the body or constructing a token list.

    The JSON parser remains responsible for syntax validation. Invalid input may
    over-reserve and be rejected; it cannot reduce the reservation for valid JSON.
    String payloads use their widest possible Python Unicode representation.
    """
    graph = total = 0
    source_width = 1
    quoted = escaped = primitive = False
    string_bytes = 0
    width = 1
    unicode_left = unicode_value = 0
    for chunk in chunks:
        total += len(chunk)
        for byte in chunk:
            if byte >= 0xF0:
                source_width = 4
            elif byte >= 0xC4:
                source_width = max(source_width, 2)
            if quoted:
                if unicode_left:
                    unicode_left -= 1
                    try:
                        unicode_value = unicode_value * 16 + int(chr(byte), 16)
                    except ValueError:
                        unicode_value = 0xFFFF
                    if not unicode_left:
                        width = max(width, 4 if 0xD800 <= unicode_value <= 0xDFFF
                                    else 2 if unicode_value > 255 else 1)
                    string_bytes += 1
                elif escaped:
                    escaped = False
                    string_bytes += 1
                    if byte == 117:  # u
                        unicode_left, unicode_value = 4, 0
                elif byte == 92:
                    escaped = True
                    string_bytes += 1
                elif byte == 34:
                    graph += 80 + string_bytes * width
                    quoted = False
                else:
                    string_bytes += 1
                    if byte >= 0xF0:
                        width = 4
                    elif byte >= 0xC4:
                        width = max(width, 2)
                continue
            if byte == 34:
                quoted, primitive = True, False
                string_bytes, width = 0, 1
            elif byte == 123:  # dict, including spare hash table slots
                graph += 256
                primitive = False
            elif byte == 91:  # list, including its initial allocation
                graph += 128
                primitive = False
            elif byte == 58:  # dict entry/table growth
                graph += 128
                primitive = False
            elif byte == 44:  # list entry plus spare capacity
                graph += 32
                primitive = False
            elif byte in b' \t\r\n]}':
                primitive = False
            else:
                if not primitive:
                    graph += 64
                    primitive = True
                # Integers can be arbitrarily wide up to Python's parse limit.
                graph += 4
    if quoted:
        graph += 80 + string_bytes * max(4, width)
    # The existing raw-body reservation covers one-byte decode workspace; widen
    # it when UTF-8 source requires a two/four-byte CPython Unicode buffer.
    return graph + total * (source_width - 1) + 256


def retained_json_bytes(value) -> int:
    """Measure an object tree without an O(number-of-nodes) auxiliary queue.

    Duplicate/interned objects are conservatively counted again. JSON payloads
    are acyclic; an adapter-created cycle is rejected by the depth guard.
    """
    frames = [iter((value,))]
    total = 0
    while frames:
        try:
            item = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            def entries(mapping):
                for key, val in mapping.items():
                    yield key
                    yield val
            frames.append(iter(entries(item)))
        elif isinstance(item, (list, tuple)):
            frames.append(iter(item))
        if len(frames) > 1024:
            raise ValueError('JSON nesting exceeds memory accounting depth')
    return total
