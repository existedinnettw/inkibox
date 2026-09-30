"""In-place editing of KiCad s-expression files: parse, change, write back only what changed."""

from __future__ import annotations

from pathlib import Path

from inkibox.kicad.sfile import SFile, format_node, node, number, parse, string

FOOTPRINT = """(footprint "R_0603"
\t(layer "F.Cu")
\t(uuid "0c27d627-50fc-4a3b-8067-8a84fbfb2a9b")
\t(attr board_only exclude_from_pos_files exclude_from_bom allow_missing_courtyard
\t\tdnp
\t)
\t(fp_poly
\t\t(pts
\t\t\t(xy -1.65 -2.65) (xy 2.65 -2.65) (xy 2.65 2.65) (xy -2.65 2.65) (xy -2.65 -1.65) (xy -1.65 -2.65)
\t\t\t(xy 0 0)
\t\t)
\t\t(layer "F.SilkS")
\t)
\t(pad "1" smd roundrect
\t\t(at -0.775 0 270)
\t\t(size 0.9 0.95)
\t\t(net "VDDA")
\t)
\t(dimension
\t\t(format
\t\t\t(units 3)
\t\t\t(precision 4) suppress_zeroes)
\t)
\t(setup
\t\t(dashed_line_dash_ratio 12.000000)
\t\t(name "a \\"quoted\\" \\\\ name")
\t)
)
"""


def test_every_item_formats_back_to_its_source():
    """
    Given a file as KiCad writes it (xy runs, a wrapped atom run, a trailing bare atom,
          %f numbers, escaped strings)
    When every node is formatted from scratch
    Then each gives back exactly its source text
    """
    root = parse(FOOTPRINT)
    for nd in root.walk():
        assert format_node(nd.copy(), nd.indent or 0) == FOOTPRINT[nd.start : nd.end], (
            nd.head
        )


def test_unchanged_file_renders_byte_identical(tmp_path: Path):
    p = tmp_path / "x.kicad_mod"
    p.write_text(FOOTPRINT)
    f = SFile.load(p)
    assert f.render() == FOOTPRINT
    assert not f.save()


def test_a_change_is_spliced_in_and_the_rest_is_kept(tmp_path: Path):
    """
    Given the file
    When the pad's net is replaced
    Then only that line differs, and "12.000000" and the escapes survive
    """
    p = tmp_path / "x.kicad_mod"
    p.write_text(FOOTPRINT)
    f = SFile.load(p)
    pad = f.root.child("pad")
    assert pad is not None
    pad.replace_child(pad.child("net"), node("net", "GND"))  # type: ignore[arg-type]
    assert f.save()
    new = p.read_text()
    assert new == FOOTPRINT.replace('(net "VDDA")', '(net "GND")')


def test_values_and_numbers():
    root = parse(FOOTPRINT)
    assert root.child("setup").value("name") == 'a "quoted" \\ name'  # type: ignore[union-attr]
    assert string('a "b"').raw == '"a \\"b\\""'
    assert (
        number(-0.0).raw == "0" and number(1.5).raw == "1.5" and number(2.0).raw == "2"
    )
    assert number(0.000001).raw == "0.000001"


def test_crlf_file_keeps_crlf(tmp_path: Path):
    """
    Given a file checked out with CRLF line endings
    When a multi-line item is replaced
    Then every line of the result ends in CRLF
    """
    p = tmp_path / "x.kicad_mod"
    p.write_bytes(FOOTPRINT.replace("\n", "\r\n").encode())
    f = SFile.load(p)
    pad = f.root.child("pad")
    assert pad is not None
    pad.replace_child(pad.child("size"), node("size", node("xy", 1, 2)))  # type: ignore[arg-type]
    f.save()
    data = p.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
