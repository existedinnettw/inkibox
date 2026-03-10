"""
CLI that calculates the area of all footprints
Displays in front:, back: and total:
"""

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path

from kipy import KiCad
from kipy.util.units import to_mm


@dataclass(frozen=True)
class ComponentArea:
    reference: str
    front_width: float
    front_height: float
    front_area: float
    back_width: float
    back_height: float
    back_area: float


def is_through_hole_component(footprint) -> bool:
    """
    Detect if a footprint is a through-hole component by checking for drill holes.
    
    Args:
        footprint: FootprintInstance object
        
    Returns:
        True if footprint contains any drilled pad (through-hole)
    """
    if not footprint.definition.pads:
        return False

    for pad in footprint.definition.pads:
        if not pad.padstack:
            continue
        drill = pad.padstack.drill
        if drill and (drill.diameter.x > 0 or drill.diameter.y > 0):
            return True

    return False


def get_footprint_bounding_box(footprint) -> tuple[float, float]:
    """
    Calculate the bounding box of a footprint from its pads.
    
    Args:
        footprint: FootprintInstance object
        
    Returns:
        Tuple of (width_mm, height_mm)
    """
    if not footprint.definition.pads:
        return 0.0, 0.0
    
    # Collect all pad positions and sizes
    positions = []
    
    for pad in footprint.definition.pads:
        pad_x = to_mm(pad.position.x)
        pad_y = to_mm(pad.position.y)
        
        # Get pad size from the padstack
        if pad.padstack and pad.padstack.copper_layers and len(pad.padstack.copper_layers) > 0:
            cl = pad.padstack.copper_layers[0]
            if hasattr(cl, 'size') and cl.size:
                pad_w = to_mm(cl.size.x)
                pad_h = to_mm(cl.size.y)
                
                # Add corner coordinates of the pad rectangle
                positions.append((pad_x - pad_w/2, pad_y - pad_h/2))
                positions.append((pad_x + pad_w/2, pad_y - pad_h/2))
                positions.append((pad_x - pad_w/2, pad_y + pad_h/2))
                positions.append((pad_x + pad_w/2, pad_y + pad_h/2))
    
    if not positions:
        return 0.0, 0.0
    
    # Find min/max coordinates
    x_coords = [p[0] for p in positions]
    y_coords = [p[1] for p in positions]
    
    width_mm = max(x_coords) - min(x_coords)
    height_mm = max(y_coords) - min(y_coords)
    
    return width_mm, height_mm


def _compute_component_area(footprint) -> ComponentArea:
    reference = footprint.reference_field.text.value
    width_mm, height_mm = get_footprint_bounding_box(footprint)
    is_through_hole = is_through_hole_component(footprint)

    front_width = width_mm
    front_height = height_mm
    front_area = width_mm * height_mm

    if is_through_hole:
        """
        TODO: consider get exact area by calculate contour of back layer.
        For now, we assume the back layer area is same as front layer which is reasonable.
        """
        back_width = width_mm
        back_height = height_mm
        back_area = width_mm * height_mm
    else:
        back_width = 0.0
        back_height = 0.0
        back_area = 0.0

    return ComponentArea(
        reference=reference,
        front_width=front_width,
        front_height=front_height,
        front_area=front_area,
        back_width=back_width,
        back_height=back_height,
        back_area=back_area,
    )


def calculate_footprint_areas() -> dict:
    """
    Calculate the area of each footprint on the placement layer and its opposite layer.
    SMT footprints only occupy the placement layer. Through-hole footprints occupy both sides.

    Returns:
        Dictionary with 'components' (list of component data with placement/opposite areas) and totals

    """
    kicad = KiCad()
    board = kicad.get_board()

    components: list[ComponentArea] = []
    front_total = 0.0
    back_total = 0.0

    for footprint in board.get_footprints():
        component = _compute_component_area(footprint)
        components.append(component)
        front_total += component.front_area
        back_total += component.back_area

    return {
        'components': components,
        'front_total': front_total,
        'back_total': back_total,
        'total': front_total + back_total
    }


def _print_component_table(components: list[ComponentArea], front_total: float, back_total: float) -> None:
    print("\nCOMPONENT FOOTPRINT AREAS:")
    print(
        f"{'Reference':<12} {'Front_W':<12} {'Front_H':<12} {'Front_A':<12} "
        f"{'Back_W':<12} {'Back_H':<12} {'Back_A':<12}"
    )
    print("-" * 96)
    for comp in components:
        print(
            f"{comp.reference:<12} {comp.front_width:<12.2f} {comp.front_height:<12.2f} "
            f"{comp.front_area:<12.2f} {comp.back_width:<12.2f} {comp.back_height:<12.2f} "
            f"{comp.back_area:<12.2f}"
        )
    print("-" * 96)
    print(f"{'TOTAL':<12} {'':<12} {'':<12} {front_total:<12.2f} {'':<12} {'':<12} {back_total:<12.2f}")


def _print_summary(front_total: float, back_total: float, total: float) -> None:
    print("\n" + "=" * 50)
    print("SUMMARY:")
    print(f"  Front:  {front_total:.2f} mm2")
    print(f"  Back:   {back_total:.2f} mm2")
    print(f"  Total:  {total:.2f} mm2")
    print("=" * 50)


def _write_csv(path: Path, components: list[ComponentArea], front_total: float, back_total: float, total: float) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "reference",
            "front_width",
            "front_height",
            "front_area",
            "back_width",
            "back_height",
            "back_area",
        ])
        for comp in components:
            writer.writerow([
                comp.reference,
                f"{comp.front_width:.2f}",
                f"{comp.front_height:.2f}",
                f"{comp.front_area:.2f}",
                f"{comp.back_width:.2f}",
                f"{comp.back_height:.2f}",
                f"{comp.back_area:.2f}",
            ])
        writer.writerow([])
        writer.writerow(["front_total", f"{front_total:.2f}"])
        writer.writerow(["back_total", f"{back_total:.2f}"])
        writer.writerow(["total", f"{total:.2f}"])


def _write_json(path: Path, components: list[ComponentArea], front_total: float, back_total: float, total: float) -> None:
    payload = {
        "components": [
            {
                "reference": comp.reference,
                "front_width": comp.front_width,
                "front_height": comp.front_height,
                "front_area": comp.front_area,
                "back_width": comp.back_width,
                "back_height": comp.back_height,
                "back_area": comp.back_area,
            }
            for comp in components
        ],
        "front_total": front_total,
        "back_total": back_total,
        "total": total,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description='Calculate the total area of footprints in the active KiCad PCB'
    )
    parser.add_argument(
        'pcb_file',
        nargs='?',
        default=None,
        help='(Optional) PCB file name for reference - script uses active board in KiCad'
    )
    parser.add_argument(
        '--csv',
        dest='csv_path',
        default=None,
        help='Write results to a CSV file (path)'
    )
    parser.add_argument(
        '--json',
        dest='json_path',
        default=None,
        help='Write results to a JSON file (path)'
    )
    
    args = parser.parse_args()
    
    if args.pcb_file:
        print("Note: Calculating for active board in KiCad")
        print(f"(Please ensure {args.pcb_file} is open)")
    else:
        print("Calculating footprint areas for active board in KiCad")
    print("-" * 50)
    
    try:
        data = calculate_footprint_areas()

        _print_component_table(
            data['components'],
            data['front_total'],
            data['back_total'],
        )
        _print_summary(
            data['front_total'],
            data['back_total'],
            data['total'],
        )

        if args.csv_path:
            _write_csv(
                Path(args.csv_path),
                data['components'],
                data['front_total'],
                data['back_total'],
                data['total'],
            )
            print(f"CSV written: {args.csv_path}")

        if args.json_path:
            _write_json(
                Path(args.json_path),
                data['components'],
                data['front_total'],
                data['back_total'],
                data['total'],
            )
            print(f"JSON written: {args.json_path}")
        
        return 0
    except Exception as e:
        print(f"Error calculating footprint areas: {e}")
        print("\nMake sure:")
        print("  1. KiCad is running")
        print("  2. A PCB file is open in KiCad")
        print("  3. kipy server is enabled in KiCad preferences")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    exit(main())