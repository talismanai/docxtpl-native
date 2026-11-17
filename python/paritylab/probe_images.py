"""Fixed probe images.

Encoded once with Pillow: an encoder (zlib, libjpeg) can produce other bytes
on another platform, and the probes record the media bytes the reference
writes. Keys are (color, (width, height), format).
"""
from __future__ import annotations

import base64

_ENCODED: dict[tuple[str, tuple[int, int], str], str] = {
    ('black', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAC0lEQVR4nGNgwAcAAB4AAfb96ZYAAAAASUVORK5CYI'
        'I='
    ),
    ('blue', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGNkYPjPAAZMEAqFBQAbXQEF2cdRlAAAAA'
        'BJRU5ErkJggg=='
    ),
    ('cyan', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGNk+P+fAQyYIBQKCwA3QQIE/7pl6QAAAA'
        'BJRU5ErkJggg=='
    ),
    ('gold', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEklEQVR4nGP8f50BApigNDILADTfAdwXB5IeAAAAAE'
        'lFTkSuQmCC'
    ),
    ('lime', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEklEQVR4nGNk+M8AAUxQGpkFABxcAQV16O5KAAAAAE'
        'lFTkSuQmCC'
    ),
    ('magenta', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGP8z/CfAQyYIBQKCwA4QAIEbuBJkAAAAA'
        'BJRU5ErkJggg=='
    ),
    ('maroon', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEUlEQVR4nGNsYIACJhgDiQUADvgAhuSfu7wAAAAASU'
        'VORK5CYII='
    ),
    ('navy', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGNkYGhgAAMmCIXCAgAN+ACGeTF5dQAAAA'
        'BJRU5ErkJggg=='
    ),
    ('olive', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEklEQVR4nGNsaGCAACYojcwCABz4AQZJ5uCGAAAAAE'
        'lFTkSuQmCC'
    ),
    ('orange', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEklEQVR4nGP8v5QBApigNDILAC9nAaqnayvaAAAAAE'
        'lFTkSuQmCC'
    ),
    ('pink', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGP8f+A0AxgwQSgUFgBHxAKQBeXn7AAAAA'
        'BJRU5ErkJggg=='
    ),
    ('purple', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGNsYGhgAAMmCIXCAgAceAEG9xF/IQAAAA'
        'BJRU5ErkJggg=='
    ),
    ('red', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEUlEQVR4nGP8zwAFTDAGEgsAHVsBBeg3qMUAAAAASU'
        'VORK5CYII='
    ),
    ('teal', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAE0lEQVR4nGNkaGhgAAMmCIXCAgAb+AEGN2kP0AAAAA'
        'BJRU5ErkJggg=='
    ),
    ('green', (3, 3), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEklEQVR4nGNkaGCAACYojcwCAA54AIZp6Rr0AAAAAE'
        'lFTkSuQmCC'
    ),
    ('red', (30, 20), 'PNG'): (
        'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAUCAIAAAAVyRqTAAAAKklEQVR4nO3QoQEAAAiEwNf9d9Ys3cYNQKAmX/qtHN'
        'PgEHAIOAQcAg7JtamaASd9Sb1QAAAAAElFTkSuQmCC'
    ),
    ('green', (3, 3), 'JPEG'): (
        '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIx'
        'wcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy'
        'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAMDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAw'
        'QFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
        'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmq'
        'KjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB'
        'AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRob'
        'HBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE'
        'hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+P'
        'n6/9oADAMBAAIRAxEAPwDiqKKK+aPjz//Z'
    ),
    ('green', (3, 3), 'GIF'): 'R0lGODdhAwADAIEAAACAAAAAAAAAAAAAACwAAAAAAwADAAAIBwABCBw4MCAAOw==',
    ('green', (3, 3), 'BMP'): (
        'Qk1aAAAAAAAAADYAAAAoAAAAAwAAAAMAAAABABgAAAAAACQAAADEDgAAxA4AAAAAAAAAAAAAAIAAAIAAAIAAAAAAAI'
        'AAAIAAAIAAAAAAAIAAAIAAAIAAAAAA'
    ),
    ('green', (3, 3), 'TIFF'): (
        'SUkqAAgAAAAKAAABBAABAAAAAwAAAAEBBAABAAAAAwAAAAIBAwADAAAAhgAAAAMBAwABAAAAAQAAAAYBAwABAAAAAg'
        'AAABEBBAABAAAAjAAAABUBAwABAAAAAwAAABYBBAABAAAAAwAAABcBBAABAAAAGwAAABwBAwABAAAAAQAAAAAAAAAI'
        'AAgACAAAgAAAgAAAgAAAgAAAgAAAgAAAgAAAgAAAgAA='
    ),
}


def image(color: str = 'red', size: tuple[int, int] = (3, 3), format: str = 'PNG') -> bytes:
    return base64.b64decode(_ENCODED[(color, size, format)])
