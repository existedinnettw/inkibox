"""
Clear every track and via on the active KiCad board using kipy.
"""

import argparse
from pathlib import Path

from kipy import KiCad
from kipy.errors import ApiError, ConnectionError


def clear_tracks_and_vias(board) -> tuple[int, int]:
	"""Delete all tracks and vias from the given board and return counts."""
	tracks = list(board.get_tracks())
	vias = list(board.get_vias())
	items = tracks + vias

	if not items:
		return 0, 0

	commit = board.begin_commit()
	try:
		board.remove_items(items)
		board.push_commit(commit, message="Clear all tracks and vias")
	except Exception:
		board.drop_commit(commit)
		raise

	board.save()
	return len(tracks), len(vias)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Clear all tracks and vias from the active KiCad board via kipy",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	parser.add_argument(
		"--yes",
		action="store_true",
		help="Do not prompt for confirmation",
	)
	parser.add_argument(
		"--log-board-path",
		metavar="PATH",
		help="Optional board path for logging only; the active KiCad board is modified",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()

	try:
		kicad = KiCad()
		print(f"Connected to KiCad {kicad.get_version()}")
		board = kicad.get_board()
	except (ApiError, ConnectionError) as exc:
		print(f"ERROR: Could not connect to KiCad: {exc}")
		return 1

	if args.log_board_path:
		print(f"Requested board path (log only): {Path(args.log_board_path).resolve()}")

	print(f"Active board: {board.name}")

	tracks = list(board.get_tracks())
	vias = list(board.get_vias())
	total = len(tracks) + len(vias)

	if total == 0:
		print("No tracks or vias found.")
		return 0

	if not args.yes:
		response = input(
			f"Remove {total} items ({len(tracks)} tracks, {len(vias)} vias)? [y/N] "
		).strip().lower()
		if response not in {"y", "yes"}:
			print("Aborted.")
			return 0

	try:
		removed_tracks, removed_vias = clear_tracks_and_vias(board)
	except (ApiError, ConnectionError) as exc:
		print(f"ERROR: Failed to remove items: {exc}")
		return 1
	except Exception as exc:
		print(f"ERROR: Unexpected failure: {exc}")
		return 1

	print("Removal complete:")
	print(f"  Tracks: {removed_tracks}")
	print(f"  Vias:   {removed_vias}")
	print(f"  Total:  {removed_tracks + removed_vias}")

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
