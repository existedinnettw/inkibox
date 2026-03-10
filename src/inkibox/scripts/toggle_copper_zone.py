"""
Toggle copper zones on and off in KiCad PCB files.

This script allows you to:
- Remove all zone (copper fill) definitions from a .kicad_pcb file
- Save the removed zones to a backup file
- Restore zones from the backup file

Usage:
    python toggle_copper_zone.py <pcb_file> off    # Remove zones and save backup
    python toggle_copper_zone.py <pcb_file> on     # Restore zones from backup
    python toggle_copper_zone.py <pcb_file> status # Check zone status

If <pcb_file> is omitted, the script searches the current directory for a
single *.kicad_pcb file and uses it automatically.

Attention: It will not refresh the KiCad GUI automatically. You may need to
close and reopen the board in KiCad to see the changes.
"""

import sys
import re
import json
import argparse
from pathlib import Path
from typing import List, Tuple


def find_pcb_file() -> Path:
    """Locate a .kicad_pcb file in the current directory when none is given."""
    pcb_files = sorted(Path.cwd().glob('*.kicad_pcb'))

    if not pcb_files:
        print("Error: No *.kicad_pcb file found in the current directory.")
        print("Specify a PCB path explicitly.")
        sys.exit(1)

    if len(pcb_files) == 1:
        print(f"Found PCB file: {pcb_files[0]}")
        return pcb_files[0]

    print("Multiple PCB files found:")
    for idx, file in enumerate(pcb_files, 1):
        print(f"  {idx}. {file.name}")

    choice = input("Select file number (or press Enter to cancel): ").strip()
    if not choice:
        print("No selection made. Exiting.")
        sys.exit(1)

    try:
        selection = int(choice)
    except ValueError:
        print("Invalid selection. Exiting.")
        sys.exit(1)

    if not 1 <= selection <= len(pcb_files):
        print("Selection out of range. Exiting.")
        sys.exit(1)

    return pcb_files[selection - 1]


def extract_zones(pcb_content: str) -> Tuple[str, List[dict]]:
    """
    Extract all zone definitions from PCB content.
    
    Returns:
        Tuple of (content without zones, list of zone data)
    """
    zones = []
    zone_pattern = r'(\t\(zone\n.*?\n\t\))\n'
    
    def extract_zone_data(match):
        zone_text = match.group(1)
        zones.append({
            'text': zone_text,
            'position': match.start()
        })
        return ''  # Remove the zone from content
    
    # Remove zones using regex, handling nested parentheses
    # We need to match complete zone blocks including nested filled_polygon sections
    lines = pcb_content.split('\n')
    result_lines = []
    zone_lines = []
    in_zone = False
    paren_depth = 0
    zone_start_idx = 0
    
    for i, line in enumerate(lines):
        if line.strip().startswith('(zone'):
            in_zone = True
            zone_start_idx = len(result_lines)
            zone_lines = [line]
            paren_depth = line.count('(') - line.count(')')
        elif in_zone:
            zone_lines.append(line)
            paren_depth += line.count('(') - line.count(')')
            
            if paren_depth == 0:
                # Zone block complete
                zone_text = '\n'.join(zone_lines)
                zones.append({
                    'text': zone_text,
                    'line_number': zone_start_idx
                })
                in_zone = False
                zone_lines = []
        else:
            result_lines.append(line)
    
    content_without_zones = '\n'.join(result_lines)
    return content_without_zones, zones


def insert_zones(pcb_content: str, zones: List[dict]) -> str:
    """
    Insert zones back into PCB content.
    
    Args:
        pcb_content: PCB file content without zones
        zones: List of zone definitions with position info
    """
    lines = pcb_content.split('\n')
    
    # Sort zones by line number in reverse to maintain positions during insertion
    sorted_zones = sorted(zones, key=lambda x: x.get('line_number', 0), reverse=True)
    
    # Find the best insertion point (before embedded_fonts or end of file)
    insertion_line = len(lines) - 2  # Before the closing parenthesis
    
    for i, line in enumerate(lines):
        if '(embedded_fonts' in line:
            insertion_line = i
            break
    
    # Insert all zones at the insertion point
    for zone in sorted_zones:
        zone_lines = zone['text'].split('\n')
        # Insert zone lines at the insertion point
        for zone_line in reversed(zone_lines):
            lines.insert(insertion_line, zone_line)
    
    return '\n'.join(lines)


def get_backup_filename(pcb_file: Path) -> Path:
    """Get the backup filename for zone data."""
    return pcb_file.parent / f".{pcb_file.stem}_zones_backup.json"


def save_zones_backup(pcb_file: Path, zones: List[dict]) -> None:
    """Save zones to a backup file."""
    backup_file = get_backup_filename(pcb_file)
    
    with open(backup_file, 'w', encoding='utf-8') as f:
        json.dump({
            'pcb_file': str(pcb_file),
            'zone_count': len(zones),
            'zones': zones
        }, f, indent=2)
    
    print(f"Saved {len(zones)} zones to {backup_file}")


def load_zones_backup(pcb_file: Path) -> List[dict]:
    """Load zones from backup file."""
    backup_file = get_backup_filename(pcb_file)
    
    if not backup_file.exists():
        raise FileNotFoundError(f"No backup file found: {backup_file}")
    
    with open(backup_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return data['zones']


def toggle_zones_off(pcb_file: Path) -> None:
    """Remove zones from PCB file and save backup."""
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_file}")
        sys.exit(1)
    
    # Read PCB file
    with open(pcb_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Extract zones
    content_without_zones, zones = extract_zones(content)
    
    if not zones:
        print("No zones found in PCB file.")
        return
    
    # Save backup
    save_zones_backup(pcb_file, zones)
    
    # Write modified PCB file
    with open(pcb_file, 'w', encoding='utf-8') as f:
        f.write(content_without_zones)
    
    print(f"Removed {len(zones)} zones from {pcb_file}")
    print("Zones have been backed up and can be restored with 'on' command.")


def toggle_zones_on(pcb_file: Path) -> None:
    """Restore zones to PCB file from backup."""
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_file}")
        sys.exit(1)
    
    # Load zones from backup
    try:
        zones = load_zones_backup(pcb_file)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("No backup found. Use 'off' command first to create a backup.")
        sys.exit(1)
    
    # Read current PCB file
    with open(pcb_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check if zones already exist
    _, existing_zones = extract_zones(content)
    if existing_zones:
        print(f"Warning: PCB file already contains {len(existing_zones)} zones.")
        response = input("Do you want to replace them? (y/n): ")
        if response.lower() != 'y':
            print("Operation cancelled.")
            return
        # Remove existing zones first
        content, _ = extract_zones(content)
    
    # Insert zones
    content_with_zones = insert_zones(content, zones)
    
    # Write modified PCB file
    with open(pcb_file, 'w', encoding='utf-8') as f:
        f.write(content_with_zones)
    
    print(f"Restored {len(zones)} zones to {pcb_file}")


def check_status(pcb_file: Path) -> None:
    """Check the current zone status."""
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_file}")
        sys.exit(1)
    
    # Read PCB file
    with open(pcb_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Extract zones
    _, zones = extract_zones(content)
    
    # Check for backup
    backup_file = get_backup_filename(pcb_file)
    has_backup = backup_file.exists()
    
    print(f"PCB File: {pcb_file}")
    print(f"Current zones in file: {len(zones)}")
    print(f"Backup exists: {'Yes' if has_backup else 'No'}")
    
    if has_backup:
        backup_zones = load_zones_backup(pcb_file)
        print(f"Zones in backup: {len(backup_zones)}")


def main():
    parser = argparse.ArgumentParser(
        description='Toggle copper zones in KiCad PCB files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python toggle_copper_zone.py myboard.kicad_pcb off     # Remove zones
    python toggle_copper_zone.py myboard.kicad_pcb on      # Restore zones
    python toggle_copper_zone.py myboard.kicad_pcb status  # Check status
    python toggle_copper_zone.py off                       # Auto-detect PCB file
        """
    )
    
    parser.add_argument('pcb_file', nargs='?', default=None, type=str,
                        help='Path to .kicad_pcb file (optional)')
    parser.add_argument('action', choices=['on', 'off', 'status'], 
                       help='Action to perform: on (restore zones), off (remove zones), status (check current state)')
    
    args = parser.parse_args()
    
    pcb_file = Path(args.pcb_file) if args.pcb_file else find_pcb_file()
    
    if args.action == 'off':
        toggle_zones_off(pcb_file)
    elif args.action == 'on':
        toggle_zones_on(pcb_file)
    elif args.action == 'status':
        check_status(pcb_file)


if __name__ == '__main__':
    main()