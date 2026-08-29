import base64
import io
import math
from typing import Any, NamedTuple

import matplotlib.patches as patches
import matplotlib.pyplot as plt
from ortools.sat.python import cp_model

_LAMBDA_SIZE = 1
_LAMBDA_WIRE = 1
_SCALE = 1e-3


class WireInfo(NamedTuple):
    source: int
    dest: int
    location_source: tuple[float, float]
    location_dest: tuple[float, float]


def pack_components_general(
    rects: list[tuple[float, float]],
    wires: list[WireInfo],
    constraints: list[bool],
) -> list[tuple[float, float]]:
    """
    Packs rectangles that minimizes some combination of:
    1) sum of wire lengths
    2) perimeter (has to be linear)

    Assumes that (0, 0) is the bottom left.

    Args:
        rects: list of (width, height) of each rectangle
        wires: list of (source, dest, location_source, location_dest) indicating that
               1) wire from rects[source] to recs[dest] (0-indexed)
               2) location_source is the point of the wire in source with respect to its bottom left
               3) location_dest is the point of the wire in dest with respect to its bottom left
        constraints: list of booleans indicating whether each rectangle must be on the edge
    Returns:
        Coordinates of the bottom left of each rectangle with the i-th coordinate corresponding
        to the i-th rectangle in the input.
    """
    if len(constraints) != len(rects):
        raise ValueError("Constraints must be the same length as rects")

    model = cp_model.CpModel()
    n = len(rects)

    # Maximum size
    x_sum = max(1, sum(math.ceil(w * _SCALE) for w, _ in rects))
    y_sum = max(1, sum(math.ceil(h * _SCALE) for h, _ in rects))

    # Basic coordinate variables
    x = [model.NewIntVar(0, x_sum, f"x[{i}]") for i in range(n)]
    y = [model.NewIntVar(0, y_sum, f"y[{i}]") for i in range(n)]

    # Overlap constrains
    x_intervals = []
    y_intervals = []

    for i, (w_true, h_true) in enumerate(rects):
        w_scaled = math.ceil(w_true * _SCALE)
        h_scaled = math.ceil(h_true * _SCALE)

        x_intervals.append(
            model.NewFixedSizeIntervalVar(x[i], w_scaled, f"x_intervals[{i}]")
        )
        y_intervals.append(
            model.NewFixedSizeIntervalVar(y[i], h_scaled, f"y_intervals[{i}]")
        )

    model.AddNoOverlap2D(x_intervals, y_intervals)

    # Size of grid determined from locations
    w_used = model.NewIntVar(0, x_sum, "w_used")
    h_used = model.NewIntVar(0, y_sum, "h_used")

    model.AddMaxEquality(
        w_used, [x[i] + math.ceil(rects[i][0] * _SCALE) for i in range(n)]
    )
    model.AddMaxEquality(
        h_used, [y[i] + math.ceil(rects[i][1] * _SCALE) for i in range(n)]
    )

    # Add edge constraints for components that must be on the edge
    for i in range(n):
        if constraints[i]:  # Only add edge constraints for components that require it
            w_scaled = math.ceil(rects[i][0] * _SCALE)
            h_scaled = math.ceil(rects[i][1] * _SCALE)

            b_left = model.NewBoolVar(f"on_left[{i}]")
            b_bottom = model.NewBoolVar(f"on_bottom[{i}]")
            b_right = model.NewBoolVar(f"on_right[{i}]")
            b_top = model.NewBoolVar(f"on_top[{i}]")

            # If bool is true then what is implied by the bool must be true
            model.Add(x[i] == 0).OnlyEnforceIf(b_left)
            model.Add(y[i] == 0).OnlyEnforceIf(b_bottom)
            model.Add(x[i] + w_scaled == w_used).OnlyEnforceIf(b_right)
            model.Add(y[i] + h_scaled == h_used).OnlyEnforceIf(b_top)

            # At least one bool must be true (component must be on at least one edge)
            model.AddBoolOr([b_left, b_bottom, b_right, b_top])

    def endpoint_expr(i: int, px: float, py: float) -> Any:
        px_scaled = round(px * _SCALE)
        py_scaled = round(py * _SCALE)
        sx = x[i] + px_scaled
        sy = y[i] + py_scaled
        return sx, sy

    wire_abs_terms = []

    for i, w in enumerate(wires):
        sx, sy = endpoint_expr(w.source, *w.location_source)
        dx, dy = endpoint_expr(w.dest, *w.location_dest)

        # Set variables to equal the wire length manhatten distance componenents
        dx_diff = model.NewIntVar(-x_sum, x_sum, f"dx_diff[{i}]")
        dy_diff = model.NewIntVar(-y_sum, y_sum, f"dy_diff[{i}]")
        model.Add(dx_diff == sx - dx)
        model.Add(dy_diff == sy - dy)

        dx_abs = model.NewIntVar(0, x_sum, f"dx_abs[{i}]")
        dy_abs = model.NewIntVar(0, y_sum, f"dy_abs[{i}]")
        model.AddAbsEquality(dx_abs, dx_diff)
        model.AddAbsEquality(dy_abs, dy_diff)

        wire_abs_terms.append(dx_abs)
        wire_abs_terms.append(dy_abs)

    # Minimize
    size_expr = w_used + h_used
    wire_expr = cp_model.LinearExpr.Sum(wire_abs_terms) if wire_abs_terms else 0
    model.Minimize(_LAMBDA_SIZE * size_expr + _LAMBDA_WIRE * wire_expr)

    # Solve
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0

    status = solver.Solve(model)

    # Get answer
    if status == cp_model.OPTIMAL:
        print("Found optimal solution")
    elif status == cp_model.FEASIBLE:
        print("Found feasible solution")
    elif status == cp_model.INFEASIBLE:
        raise RuntimeError("Problem is infeasible - no valid packing exists")
    elif status == cp_model.MODEL_INVALID:
        raise RuntimeError("Model is invalid - check constraints")
    else:
        raise RuntimeError(f"Solver failed with status: {status}")

    # Construct solution
    sol = []

    for i in range(len(rects)):
        xi = solver.Value(x[i]) / _SCALE
        yi = solver.Value(y[i]) / _SCALE
        sol.append((xi, yi))

    sol.append(
        (
            math.ceil(solver.Value(w_used) / _SCALE),
            math.ceil(solver.Value(h_used) / _SCALE),
        )
    )
    return sol


def generate_visualization(
    rects: list[tuple[float, float]],
    locations: list[tuple[float, float]],
    wires: list[WireInfo] | None = None,
    names: list[str] | None = None,
) -> str:
    """
    Generate a visualization of the packed rectangles and return as base64 encoded image.

    Args:
        rects: List of (width, height) for each rectangle
        locations: List of (x, y) positions for bottom-left corner of each rectangle
        wires: Optional list of wire connections
        names: Optional list of names for each rectangle

    Returns:
        Base64 encoded PNG image string
    """
    # Create figure with white background
    fig, ax = plt.subplots(1, 1, figsize=(10, 10), facecolor="white")
    ax.set_facecolor("white")

    # Define green color palette similar to the example
    green_colors = [
        "#8FBC8F",  # Dark sea green
        "#90EE90",  # Light green
        "#98FB98",  # Pale green
        "#7CFC00",  # Lawn green
        "#ADFF2F",  # Green yellow
        "#9ACD32",  # Yellow green
        "#6B8E23",  # Olive drab
        "#8FBC8F",  # Dark sea green (repeat for more rectangles)
    ]

    # Draw rectangles
    for i, ((x, y), (w, h)) in enumerate(zip(locations, rects)):
        # Create rectangle with green fill and black border
        rect = patches.Rectangle(
            (x, y),
            w,
            h,
            linewidth=2.5,
            edgecolor="black",
            facecolor=green_colors[i % len(green_colors)],
            alpha=0.85,
            zorder=2,
        )
        ax.add_patch(rect)

        # Add label if names are provided
        if names and i < len(names):
            # Add text label in the center of the rectangle
            ax.text(
                x + w / 2,
                y + h / 2,
                names[i],
                ha="center",
                va="center",
                fontsize=10,
                fontweight="bold",
                color="black",
                zorder=3,
            )

    # Draw wires if provided
    if wires:
        for wire in wires:
            if wire.source < len(locations) and wire.dest < len(locations):
                src_pos = locations[wire.source]
                dest_pos = locations[wire.dest]

                # Calculate wire endpoints
                src_x = src_pos[0] + wire.location_source[0]
                src_y = src_pos[1] + wire.location_source[1]
                dest_x = dest_pos[0] + wire.location_dest[0]
                dest_y = dest_pos[1] + wire.location_dest[1]

                # Draw wire as black line
                ax.plot(
                    [src_x, dest_x],
                    [src_y, dest_y],
                    "k-",
                    linewidth=1.5,
                    alpha=0.9,
                    zorder=1,
                )

                # Add small dots at wire endpoints
                ax.plot(src_x, src_y, "ko", markersize=3, zorder=1)
                ax.plot(dest_x, dest_y, "ko", markersize=3, zorder=1)

    # Calculate bounds for the plot
    if locations and rects:
        all_x = [loc[0] for loc in locations] + [
            loc[0] + rect[0] for loc, rect in zip(locations, rects)
        ]
        all_y = [loc[1] for loc in locations] + [
            loc[1] + rect[1] for loc, rect in zip(locations, rects)
        ]

        padding = 0.5
        ax.set_xlim(min(all_x) - padding, max(all_x) + padding)
        ax.set_ylim(min(all_y) - padding, max(all_y) + padding)

    # Set equal aspect ratio for proper rectangle shapes
    ax.set_aspect("equal")

    # Remove axis labels and ticks for cleaner look
    ax.set_xticks([])
    ax.set_yticks([])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)

    # Tight layout
    plt.tight_layout(pad=0.5)

    # Save to bytes buffer
    buffer = io.BytesIO()
    plt.savefig(
        buffer,
        format="png",
        dpi=150,
        bbox_inches="tight",
        facecolor="white",
        edgecolor="none",
    )
    plt.close(fig)

    # Encode to base64
    buffer.seek(0)
    image_base64 = base64.b64encode(buffer.read()).decode("utf-8")

    return image_base64


# Testing
if __name__ == "__main__":
    import random

    import matplotlib.patches as patches
    import matplotlib.pyplot as plt
    import numpy as np

    # Set random seed for reproducible results
    random.seed(42)
    np.random.seed(42)

    print("Creating visualization test with 8 rectangles and 20 wire connections...")

    # Start with complex test case
    simple_test = False

    if simple_test:
        print("🧪 Testing simple case: 3 rectangles, few wires")
        rects = [
            (2.0, 1.0),  # Rectangle 0
            (1.5, 1.5),  # Rectangle 1
            (1.0, 2.0),  # Rectangle 2
        ]
        rot = [False, False, False]  # No rotation for simplicity
        wire_connections = [
            (0, 1),  # Just a few connections
            (1, 2),
        ]
    else:
        print("🧪 Testing complex case: 8 rectangles, 20 wires")
        # Create 8 rectangles of varying sizes (simulating different electronic components)
        rects = [
            (3.0, 2.0),  # Rectangle 0 - Large component (e.g., microcontroller)
            (2.5, 1.5),  # Rectangle 1 - Medium component (e.g., voltage regulator)
            (2.0, 2.5),  # Rectangle 2 - Tall component (e.g., capacitor bank)
            (1.5, 1.8),  # Rectangle 3 - Small component (e.g., crystal oscillator)
            (2.2, 1.2),  # Rectangle 4 - Wide component (e.g., connector)
            (1.8, 2.2),  # Rectangle 5 - Square-ish component (e.g., IC)
            (2.8, 1.4),  # Rectangle 6 - Medium-large component (e.g., power module)
            (1.2, 2.0),  # Rectangle 7 - Narrow component (e.g., sensor)
        ]

        # Allow rotation for some rectangles (mix of rotatable and fixed)
        rot = [True, False, True, False, True, False, True, False]

        # Create 20 wire connections between rectangles
        wire_connections = [
            # Main hub connections (Rectangle 0 as central hub)
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),  # Hub to 4 components
            # Secondary hub connections (Rectangle 1 as secondary hub)
            (1, 2),
            (1, 5),
            (1, 6),  # Secondary hub connections
            # Power distribution connections
            (2, 3),
            (2, 4),
            (2, 7),  # Power rail connections
            # Signal chain connections
            (3, 4),
            (4, 5),
            (5, 6),
            (6, 7),  # Sequential signal chain
            # Cross connections for complex routing
            (0, 5),
            (0, 7),  # Hub to distant components
            (1, 7),
            (3, 6),  # Cross connections
            (2, 6),
            (4, 7),  # Additional cross connections
        ]

    # Create wire connections between rectangles
    # Each wire connects the center of one rectangle to the center of another
    wires = []

    # Create WireInfo objects
    for src, dest in wire_connections:
        src_w, src_h = rects[src]
        dest_w, dest_h = rects[dest]

        # Connect centers of rectangles
        wires.append(
            WireInfo(
                source=src,
                dest=dest,
                location_source=(src_w / 2, src_h / 2),  # center of source
                location_dest=(dest_w / 2, dest_h / 2),  # center of dest
            )
        )

    print(f"Generated {len(rects)} rectangles and {len(wires)} wires")

    # Define component names for labeling
    if simple_test:
        component_names = ["MCU", "VReg", "Cap"]
        constraints = [False, False, False]  # No edge constraints for simple test
    else:
        component_names = [
            "MCU",  # Rectangle 0 - Microcontroller
            "VReg",  # Rectangle 1 - Voltage regulator
            "CapBank",  # Rectangle 2 - Capacitor bank
            "Crystal",  # Rectangle 3 - Crystal oscillator
            "Connector",  # Rectangle 4 - Connector
            "IC",  # Rectangle 5 - IC
            "PowerMod",  # Rectangle 6 - Power module
            "Sensor",  # Rectangle 7 - Sensor
        ]
        # Define edge constraints (components that should be on the edge)
        constraints = [
            False,  # MCU - can be anywhere
            False,  # VReg - can be anywhere
            False,  # CapBank - can be anywhere
            False,  # Crystal - can be anywhere
            True,  # Connector - should be on edge
            False,  # IC - can be anywhere
            True,  # PowerMod - should be on edge
            True,  # Sensor - should be on edge
        ]

    print(f"Edge constraints: {constraints}")

    # Pack the rectangles
    try:
        positions = pack_components_general(rects, wires, constraints)
        print("✅ Packing successful!")
        print(f"Rectangle positions: {positions}")

        # Skip the original single plot - go directly to statistics and double plot

        # Print some statistics
        total_area = sum(w * h for w, h in rects)
        bounding_box_w = max(pos[0] + rect[0] for pos, rect in zip(positions, rects))
        bounding_box_h = max(pos[1] + rect[1] for pos, rect in zip(positions, rects))
        bounding_area = bounding_box_w * bounding_box_h
        efficiency = (total_area / bounding_area) * 100 if bounding_area > 0 else 0

        print(f"\n📊 Packing Statistics:")
        print(f"Total rectangle area: {total_area:.2f}")
        print(f"Bounding box: {bounding_box_w:.2f} × {bounding_box_h:.2f}")
        print(f"Bounding box area: {bounding_area:.2f}")
        print(f"Packing efficiency: {efficiency:.1f}%")

        # Calculate total wire length
        total_wire_length = 0
        for wire in wires:
            if wire.source != wire.dest:  # Skip self-connections
                src_pos = positions[wire.source]
                dest_pos = positions[wire.dest]
                src_x = src_pos[0] + wire.location_source[0]
                src_y = src_pos[1] + wire.location_source[1]
                dest_x = dest_pos[0] + wire.location_dest[0]
                dest_y = dest_pos[1] + wire.location_dest[1]

                wire_length = abs(src_x - dest_x) + abs(
                    src_y - dest_y
                )  # Manhattan distance
                total_wire_length += wire_length

        print(f"Total wire length (Manhattan): {total_wire_length:.2f}")

        # Test the new generate_visualization function
        print("\n🎨 Testing generate_visualization function...")
        try:
            image_base64 = generate_visualization(
                rects=rects, locations=positions, wires=wires, names=component_names
            )

            print(f"✅ Generated base64 image (length: {len(image_base64)} chars)")

            # Save the image to file for testing
            import base64
            import io

            from PIL import Image

            image_data = base64.b64decode(image_base64)

            # Display the base64 image alongside the original matplotlib plot
            print("🖼️ Displaying base64 generated image...")

            # Create a new figure to show the base64 image
            fig2, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))

            # Left side: Original matplotlib visualization (simplified)
            ax1.set_title(
                "Original Matplotlib Visualization", fontsize=14, fontweight="bold"
            )
            colors = [
                "lightblue",
                "lightgreen",
                "lightcoral",
                "lightyellow",
                "lightpink",
                "lightgray",
                "lightcyan",
                "lavender",
            ]

            for i, ((x, y), (w, h)) in enumerate(zip(positions, rects)):
                rect = patches.Rectangle(
                    (x, y),
                    w,
                    h,
                    linewidth=2,
                    edgecolor="black",
                    facecolor=colors[i % len(colors)],
                    alpha=0.7,
                )
                ax1.add_patch(rect)
                ax1.text(
                    x + w / 2,
                    y + h / 2,
                    component_names[i],
                    ha="center",
                    va="center",
                    fontsize=10,
                    fontweight="bold",
                )

            for wire in wires:
                src_pos = positions[wire.source]
                dest_pos = positions[wire.dest]
                src_x = src_pos[0] + wire.location_source[0]
                src_y = src_pos[1] + wire.location_source[1]
                dest_x = dest_pos[0] + wire.location_dest[0]
                dest_y = dest_pos[1] + wire.location_dest[1]
                ax1.plot([src_x, dest_x], [src_y, dest_y], "r-", linewidth=1, alpha=0.6)

            ax1.set_aspect("equal")
            all_x = [pos[0] for pos in positions] + [
                pos[0] + rect[0] for pos, rect in zip(positions, rects)
            ]
            all_y = [pos[1] for pos in positions] + [
                pos[1] + rect[1] for pos, rect in zip(positions, rects)
            ]
            padding = 0.5
            ax1.set_xlim(min(all_x) - padding, max(all_x) + padding)
            ax1.set_ylim(min(all_y) - padding, max(all_y) + padding)
            ax1.grid(True, alpha=0.3)

            # Right side: Base64 generated image
            ax2.set_title("Base64 Generated Image", fontsize=14, fontweight="bold")

            # Load and display the base64 image
            image_buffer = io.BytesIO(image_data)
            pil_image = Image.open(image_buffer)
            ax2.imshow(pil_image)
            ax2.axis("off")  # Remove axes for cleaner look

            plt.tight_layout()
            plt.show()

            # Print first 100 characters of base64 for verification
            print(f"Base64 preview: {image_base64[:100]}...")
            print(f"Full base64 length: {len(image_base64)} characters")

        except Exception as viz_error:
            print(f"❌ Visualization generation failed: {viz_error}")
            import traceback

            traceback.print_exc()

    except Exception as e:
        print(f"❌ Packing failed: {e}")
        import traceback

        traceback.print_exc()
