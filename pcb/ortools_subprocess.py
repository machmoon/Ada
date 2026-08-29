#!/usr/bin/env python3
"""
Subprocess wrapper for ortools operations to avoid DLL conflicts with KiCad.
"""

import json
import sys
from pathlib import Path


def run_packing_operation(rects, wires_data, constraints):
    """Run packing operation in subprocess-safe way."""
    try:
        # Import ortools modules
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from packing.rectangles import WireInfo, pack_components_general

        # Convert wire data back to WireInfo objects
        wires = []
        for wire_data in wires_data:
            wires.append(
                WireInfo(
                    source=wire_data["source"],
                    dest=wire_data["dest"],
                    location_source=tuple(wire_data["location_source"]),
                    location_dest=tuple(wire_data["location_dest"]),
                )
            )

        # Run the packing
        positions = pack_components_general(rects, wires, constraints)

        return {
            "success": True,
            "positions": positions,
            "message": "Packing completed successfully",
        }

    except Exception as e:
        return {"success": False, "error": str(e), "message": "Packing failed"}


def test_ortools():
    """Simple test to verify ortools is working."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from packing.rectangles import WireInfo, pack_components_general

        # Simple test case
        rects = [(2.0, 1.0), (1.5, 1.5)]
        wires = [
            WireInfo(
                source=0, dest=1, location_source=(1.0, 0.5), location_dest=(0.75, 0.75)
            )
        ]
        constraints = [False, False]

        positions = pack_components_general(rects, wires, constraints)

        return {
            "success": True,
            "positions": positions,
            "message": "ortools test successful",
        }

    except Exception as e:
        return {"success": False, "error": str(e), "message": "ortools test failed"}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        operation = sys.argv[1]

        if operation == "test":
            result = test_ortools()
            print(json.dumps(result))

        elif operation == "pack":
            # Read input data from stdin
            input_data = json.loads(sys.stdin.read())
            result = run_packing_operation(
                input_data["rects"], input_data["wires"], input_data["constraints"]
            )
            print(json.dumps(result))

        else:
            print(
                json.dumps(
                    {"success": False, "error": f"Unknown operation: {operation}"}
                )
            )
    else:
        # Default to test
        result = test_ortools()
        print(json.dumps(result))
