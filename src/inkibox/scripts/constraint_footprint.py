from pathlib import Path
import sys

from kipy import KiCad
from kipy.board_types import BoardRectangle
from kipy.geometry import Vector2
from kipy.util.units import to_mm
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.errors import ApiError, ConnectionError

# TODO: the best algorithm for constraint is graph with DoF, rather than tree. This allows more flexible constraints (e.g. J3 and J4 can be swapped, but both must be at the same distance from J2). For now, we use a simple tree structure.


def apply_constraints(constraint_tree, board_path=None):
    """
    Connect to KiCad via kipy, apply all footprint constraints, and save.

    Args:
        constraint_tree (dict): Tree structure defining footprint constraints.
        board_path (Optional[str]): Optional board path (logged for clarity; the
            active KiCad board is used)

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        requested_board = str(Path(board_path).expanduser().resolve()) if board_path else None

        kicad = KiCad()
        print(f"Connected to KiCad {kicad.get_version()}")

        board = kicad.get_board()
        if requested_board:
            print(f"Requested board path: {requested_board}")
        print(f"Active board: {board.name}")

        def find_footprint(ref):
            return next(
                (fp for fp in board.get_footprints() if fp.reference_field.text.value == ref),
                None,
            )

        def get_board_outline_origin():
            """
            Calculate the upper-left corner (min X, min Y) of the board outline
            from Edge.Cuts shapes. Only considers BoardRectangle shapes.

            Returns:
                tuple: (x_mm, y_mm) of upper-left corner, or (None, None) if not found
            """
            shapes = board.get_shapes()
            edge_cuts_rectangles = [
                s
                for s in shapes
                if isinstance(s, BoardRectangle) and s.layer == BoardLayer.BL_Edge_Cuts
            ]

            if not edge_cuts_rectangles:
                return None, None

            min_x = min(to_mm(min(r.top_left.x, r.bottom_right.x)) for r in edge_cuts_rectangles)
            min_y = min(to_mm(min(r.top_left.y, r.bottom_right.y)) for r in edge_cuts_rectangles)

            return min_x, min_y

        # Get board outline origin
        board_origin_x_mm, board_origin_y_mm = get_board_outline_origin()

        if board_origin_x_mm is None:
            print("ERROR: Could not find Edge.Cuts layer to determine board origin")
            return False

        print("Board outline upper-left corner:")
        print(f"  X: {board_origin_x_mm:.4f} mm")
        print(f"  Y: {board_origin_y_mm:.4f} mm")

        footprints_to_update = []

        def process_constraint_node(node, parent_x_mm, parent_y_mm, parent_label, level=0):
            """
            Recursively process constraint tree nodes.

            Args:
                node: Current tree node (dict with reference, offset, children)
                parent_x_mm: Parent's X position in mm
                parent_y_mm: Parent's Y position in mm
                parent_label: Label of parent for logging
                level: Current tree depth (0=root, 1=first level, etc.)
            """
            reference = node["reference"]
            offset_x_mm, offset_y_mm = node["offset"]

            # Skip the root node (board origin)
            if reference is None:
                # Process children with board origin as parent
                for child in node["children"]:
                    process_constraint_node(
                        child, parent_x_mm, parent_y_mm, "board origin", level + 1
                    )
                return

            # Find target footprint
            footprint = find_footprint(reference)
            if not footprint:
                raise ValueError(f"Footprint '{reference}' not found on active board")

            # Calculate target position relative to parent
            target_x_mm = parent_x_mm + offset_x_mm
            target_y_mm = parent_y_mm + offset_y_mm

            # Log current and target positions
            current_x_mm = to_mm(footprint.position.x)
            current_y_mm = to_mm(footprint.position.y)

            indent = "  " * level
            print(f"\n{indent}Level {level}: {reference}")
            print(f"{indent}Parent ({parent_label}) position:")
            print(f"{indent}  X: {parent_x_mm:.4f} mm")
            print(f"{indent}  Y: {parent_y_mm:.4f} mm")

            print(f"{indent}Current position:")
            print(f"{indent}  X: {current_x_mm:.4f} mm")
            print(f"{indent}  Y: {current_y_mm:.4f} mm")

            print(f"{indent}Setting position:")
            print(f"{indent}  X: {target_x_mm:.4f} mm ({parent_label} + {offset_x_mm:.4f})")
            print(f"{indent}  Y: {target_y_mm:.4f} mm ({parent_label} + {offset_y_mm:.4f})")

            # Update footprint position
            footprint.position = Vector2.from_xy_mm(target_x_mm, target_y_mm)
            footprints_to_update.append(footprint)

            # Process children recursively
            for child in node["children"]:
                process_constraint_node(child, target_x_mm, target_y_mm, reference, level + 1)

        # Process the constraint tree starting from root
        try:
            process_constraint_node(
                constraint_tree, board_origin_x_mm, board_origin_y_mm, "board origin"
            )
        except ValueError as e:
            print(f"ERROR: {str(e)}")
            return False
        except KeyError as e:
            print(
                f"ERROR: Malformed constraint node — missing required key {e}. Each node must have 'reference', 'offset', and 'children'."
            )
            return False

        # Commit and save
        commit = board.begin_commit()
        try:
            board.update_items(footprints_to_update)
            board.push_commit(commit, message="Apply footprint constraints")
        except Exception:
            board.drop_commit(commit)
            raise

        print(f"\nSaving board: {board.name}")
        board.save()

        # Count total nodes (excluding root)
        def count_nodes(node):
            if node["reference"] is None:
                return sum(count_nodes(child) for child in node["children"])
            return 1 + sum(count_nodes(child) for child in node["children"])

        total_constraints = count_nodes(constraint_tree)
        print(f"\n✓ Success! Applied {total_constraints} constraint(s) from tree structure.")
        return True

    except (ApiError, ConnectionError, Exception) as e:
        print(f"ERROR: {str(e)}")
        return False
