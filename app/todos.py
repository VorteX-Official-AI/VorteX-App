"""Task lists the model emits (```vortex:todo).

The UI ticks these one at a time as real work finishes and removes the card
when every item is done.
"""

import re

TODO_BLOCK = re.compile(r"```vortex:todo\s*\r?\n(.*?)```", re.S | re.I)
ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*(?:\[[ xX]\]\s*)?(.+?)\s*$")
MAX_ITEMS = 8


def parse_todos(text: str) -> tuple[list[str], str]:
    """Return the task list and the reply with the todo block removed."""
    items: list[str] = []
    for block in TODO_BLOCK.findall(text or ""):
        for line in block.splitlines():
            match = ITEM.match(line)
            if not match:
                continue
            item = match.group(1).strip()[:100]
            if item and item not in items:
                items.append(item)
    return items[:MAX_ITEMS], TODO_BLOCK.sub("", text or "").strip()
