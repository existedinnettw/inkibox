"""Edit KiCad s-expression files in place: parse with source spans, change items, write
back only what changed.

KiCad files are re-read by humans in diffs, so an edit must not reformat the file:
:class:`SFile` keeps the original text and splices in only the items that were replaced,
inserted or removed. Atoms keep their exact source text (``12.000000`` stays ``12.000000``,
``"a\\"b"`` keeps its escapes), so a parsed item formats back to its original bytes.

:func:`format_node` writes an item the way KiCad 10's formatter does (``KICAD_FORMAT::
Prettify``): a list without sub-lists on one line; otherwise the head and atoms on the
first line, each sub-list on its own line one tab deeper, and ``)`` on a line of its own;
consecutive ``(xy …)`` lists share a line while the tabs of the indent plus the line
length stay below 99; a long run of atoms wraps below 72 (``)`` then on its own line); an
atom after a sub-list (KiCad 7's bare ``hide``) stays on that sub-list's line.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

XY_WRAP = 99
ATOM_WRAP = 72


class SExprError(ValueError):
    pass


# --------------------------------------------------------------------------- atoms


@dataclass(frozen=True, slots=True)
class Atom:
    """A symbol, number or string exactly as written (``raw``)."""

    raw: str

    @property
    def is_string(self) -> bool:
        return self.raw.startswith('"')

    @property
    def text(self) -> str:
        """The value: a string unquoted and unescaped, anything else as written."""
        if not self.is_string:
            return self.raw
        body = self.raw[1:-1]
        if "\\" not in body:
            return body
        out: list[str] = []
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)

    def __str__(self) -> str:
        return self.text


def string(value: str) -> Atom:
    """A quoted string atom, escaped the way KiCad writes strings."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return Atom(f'"{esc}"')


def symbol(name: str) -> Atom:
    return Atom(name)


def number(value: float) -> Atom:
    """A number the way KiCad writes one: no exponent, no trailing zeros, no ``-0``."""
    if isinstance(value, int):
        return Atom(str(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return Atom("0" if text in ("-0", "") else text)


# --------------------------------------------------------------------------- nodes


@dataclass(eq=False, slots=True)
class Node:
    """``(head item …)``; items are :class:`Atom` or :class:`Node`. ``start``/``end`` are
    the source span (``end`` exclusive) of a parsed node, ``None`` for a built one."""

    head: str
    items: list[Atom | Node] = field(default_factory=list)
    start: int | None = None
    end: int | None = None
    # set by parse(): the items as parsed (atoms by text, sub-lists by identity) and the
    # tabs before the node; a node whose items still match is copied from the source
    orig: tuple | None = field(default=None, repr=False)
    indent: int | None = field(default=None, repr=False)

    # ------------------------------------------------------------ access
    def atoms(self) -> list[Atom]:
        return [i for i in self.items if isinstance(i, Atom)]

    def atom(self, index: int = 0) -> str | None:
        """The value of the ``index``-th atom, or ``None``."""
        a = self.atoms()
        return a[index].text if index < len(a) else None

    def value(self, head: str, default: str = "", index: int = 0) -> str:
        """The ``index``-th atom of the first ``(head …)`` child, or ``default``."""
        c = self.child(head)
        v = c.atom(index) if c is not None else None
        return default if v is None else v

    def children(self, head: str | None = None) -> list[Node]:
        return [
            i
            for i in self.items
            if isinstance(i, Node) and (head is None or i.head == head)
        ]

    def child(self, head: str) -> Node | None:
        for i in self.items:
            if isinstance(i, Node) and i.head == head:
                return i
        return None

    def walk(self) -> Iterator[Node]:
        yield self
        for c in self.children():
            yield from c.walk()

    # ------------------------------------------------------------ change
    def set_atom(self, index: int, value: Atom) -> None:
        seen = -1
        for i, item in enumerate(self.items):
            if isinstance(item, Atom):
                seen += 1
                if seen == index:
                    self.items[i] = value
                    return
        self.items.append(value)

    def replace_child(self, old: Node, new: Node | None) -> None:
        for i, item in enumerate(self.items):
            if item is old:
                if new is None:
                    del self.items[i]
                else:
                    self.items[i] = new
                return
        raise ValueError(f"({old.head} …) is not a child of ({self.head} …)")

    def copy(self) -> Node:
        """A deep copy without source spans (formats from scratch)."""
        return Node(
            self.head, [i.copy() if isinstance(i, Node) else i for i in self.items]
        )

    # ------------------------------------------------------------ compare
    def key(self, *, ignore: frozenset[str] = frozenset()) -> tuple:
        """A hashable value: equal keys format to equal text. Sub-lists whose head is in
        ``ignore`` (``uuid``, say) are left out."""
        return (
            self.head,
            tuple(
                i.raw if isinstance(i, Atom) else i.key(ignore=ignore)
                for i in self.items
                if not (isinstance(i, Node) and i.head in ignore)
            ),
        )


def node(head: str, *items: Atom | Node | str | float) -> Node:
    """Build a node; ``str`` becomes a quoted string, numbers KiCad numbers."""
    out: list[Atom | Node] = []
    for i in items:
        if isinstance(i, (Atom, Node)):
            out.append(i)
        elif isinstance(i, bool):
            out.append(symbol("yes" if i else "no"))
        elif isinstance(i, str):
            out.append(string(i))
        else:
            out.append(number(i))
    return Node(head, out)


# --------------------------------------------------------------------------- parse


def parse(text: str) -> Node:
    """The single top-level list of a KiCad file, every node with its span."""
    stack: list[Node] = []
    root: Node | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            j = i + 1
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            if j == i + 1:
                raise SExprError(f"list without a head at offset {i}")
            nd = Node(text[i + 1 : j], start=i)
            if stack:
                stack[-1].items.append(nd)
            elif root is None:
                root = nd
            else:
                raise SExprError(f"second top-level list at offset {i}")
            stack.append(nd)
            i = j
        elif c == ")":
            if not stack:
                raise SExprError(f"unbalanced ')' at offset {i}")
            nd = stack.pop()
            nd.end = i + 1
            nd.orig = _shape(nd)
            assert nd.start is not None
            line_start = text.rfind("\n", 0, nd.start) + 1
            prefix = text[line_start : nd.start]
            nd.indent = len(prefix) - len(prefix.lstrip("\t"))
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise SExprError(f"unterminated string at offset {i}")
            if not stack:
                raise SExprError(f"atom outside a list at offset {i}")
            stack[-1].items.append(Atom(text[i : j + 1]))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            if not stack:
                raise SExprError(f"atom outside a list at offset {i}")
            stack[-1].items.append(Atom(text[i:j]))
            i = j
    if stack:
        raise SExprError(f"unclosed ({stack[-1].head} …)")
    if root is None:
        raise SExprError("no s-expression")
    return root


# --------------------------------------------------------------------------- format


def format_node(nd: Node, indent: int = 0) -> str:
    """``nd`` as KiCad writes it, the first line starting at ``indent`` tabs (the first
    line itself is not indented: it continues wherever the caller puts it)."""
    return _format_with(nd, None, indent)


# --------------------------------------------------------------------------- files


def read_text(path: Path) -> str:
    """A KiCad file's text with its line endings as they are (no ``\r\n`` translation:
    rewriting a file must not change every line on Windows)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


@dataclass(slots=True)
class SFile:
    """A parsed KiCad file whose changes are written back as splices."""

    path: Path
    text: str
    root: Node

    @classmethod
    def load(cls, path: Path) -> SFile:
        text = read_text(path)
        return cls(path, text, parse(text))

    def render(self) -> str:
        """The file with every changed item re-formatted and everything else as it was."""
        root = self.root
        assert root.start is not None and root.end is not None
        out = (
            self.text[: root.start]
            + _render(root, self.text, 0)
            + self.text[root.end :]
        )
        if "\r\n" in self.text:  # a CRLF checkout: inserted lines follow the file
            out = out.replace("\r\n", "\n").replace("\n", "\r\n")
        return out

    def changed(self) -> bool:
        return self.render() != self.text

    def save(self) -> bool:
        """Write the file if it changed; ``True`` if it did."""
        new = self.render()
        if new == self.text:
            return False
        write_text(self.path, new)
        self.text = new
        self.root = parse(new)
        return True


def _shape(nd: Node) -> tuple:
    return tuple(i.raw if isinstance(i, Atom) else id(i) for i in nd.items)


def _render(nd: Node, text: str, indent: int) -> str:
    """``nd`` as text: copied from the source where it is unchanged (children rendered in
    place), formatted where it changed or was built."""
    if nd.start is None or nd.end is None:
        return _format_with(nd, text, indent)
    if nd.orig != _shape(nd):
        return _format_with(nd, text, indent)
    out: list[str] = []
    pos = nd.start
    for c in nd.children():
        assert c.start is not None and c.end is not None
        out.append(text[pos : c.start])
        out.append(_render(c, text, indent + 1))
        pos = c.end
    out.append(text[pos : nd.end])
    return "".join(out)


def _format_with(nd: Node, text: str | None, indent: int) -> str:
    """KiCad layout of ``nd``; with ``text``, unchanged parsed children are copied."""
    pad = "\t" * (indent + 1)
    lines = ["(" + nd.head]
    pos = 0
    # leading atoms: wrapped once the line (tabs counted as one column) reaches 72
    while pos < len(nd.items) and isinstance(nd.items[pos], Atom):
        item = nd.items[pos]
        assert isinstance(item, Atom)
        raw = item.raw
        col = (indent if len(lines) == 1 else indent + 1) + len(lines[-1])
        if col < ATOM_WRAP or lines[-1].endswith("("):
            lines[-1] += " " + raw
        else:
            lines.append(raw)
        pos += 1
    tail = nd.items[pos:]
    if not tail:
        if len(lines) == 1:
            return lines[0] + ")"
        return ("\n" + pad).join(lines) + "\n" + "\t" * indent + ")"
    out = [("\n" + pad).join(lines)]
    cur: str | None = None  # the pending (xy …) line
    last_atom = False
    for item in tail:
        if isinstance(item, Atom):
            # an atom after a sub-list (KiCad 7's bare `hide`) stays on that line
            if cur is not None:
                out.append("\n" + pad + cur)
                cur = None
            out.append(" " + item.raw)
            last_atom = True
            continue
        last_atom = False
        if item.head == "xy" and not item.children():
            t = _format_with(item, None, indent + 1)
            if cur is not None and indent + 1 + len(cur) < XY_WRAP:
                cur += " " + t
                continue
            if cur is not None:
                out.append("\n" + pad + cur)
            cur = t
            continue
        if cur is not None:
            out.append("\n" + pad + cur)
            cur = None
        out.append("\n" + pad)
        out.append(
            _render(item, text, indent + 1)
            if text is not None
            else _format_with(item, None, indent + 1)
        )
    if cur is not None:
        out.append("\n" + pad + cur)
    out.append(")" if last_atom else "\n" + "\t" * indent + ")")
    return "".join(out)
