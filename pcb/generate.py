import base64
import io
import os as _os
import sys as _sys

import matplotlib.patches as patches
import matplotlib.pyplot as plt
from PIL import Image

_os.add_dll_directory(r"C:\Program Files\KiCad\9.0\bin")
_sys.path.append(r"C:\Program Files\KiCad\9.0\bin\Lib\site-packages")

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pcbnew

_BB_PADDING = 0


@dataclass
class SingleItemInfo:
    size: tuple[float, float]
    name: str


def get_fp_rect(fp):
    # Start with None and grow the rect
    return fp.GetBoundingBox()


def get_items_infos(pcb_path: Path, refs: list[str]) -> list[SingleItemInfo]:
    board = pcbnew.LoadBoard(pcb_path)
    sizes = []

    # Get all available footprint references for debugging
    all_footprints = list(board.GetFootprints())
    available_refs = [fp.GetReference() for fp in all_footprints]
    print(f"Available footprint references: {sorted(available_refs)}")
    print(f"Looking for: {refs}")

    for r in refs:
        f = board.FindFootprintByReference(r)

        if f is None:
            print(f"⚠️ Warning: Footprint '{r}' not found on PCB")
            continue

        bbox = get_fp_rect(f)
        width = bbox.GetWidth() + _BB_PADDING
        height = bbox.GetHeight() + _BB_PADDING

        # Try different methods to get a meaningful name for the footprint
        try:
            name = f.GetFPID().GetLibItemName().GetUTF8()  # Library footprint name
        except:
            try:
                name = f.GetReference()  # Component reference (e.g., U1, R1)
            except:
                name = "Unknown"

        sizes.append(SingleItemInfo(size=(width, height), name=name))
        print(f"✅ Found '{r}': {width}x{height} ({name})")

    return sizes


def pack_components_via_subprocess(rects, wires_data, constraints):
    """
    Call pack_components_general via subprocess to avoid DLL conflicts.

    Args:
        rects: list of (width, height) tuples
        wires_data: list of tuples (source_idx, dest_idx, source_location, dest_location)
        constraints: list of booleans for edge constraints

    Returns:
        list of (x, y) positions for each rectangle, or None if failed
    """
    import json
    import subprocess
    import sys

    # Convert wire data to the format expected by subprocess
    wire_objects = []
    for source, dest, loc_source, loc_dest in wires_data:
        wire_objects.append(
            {
                "source": source,
                "dest": dest,
                "location_source": loc_source,
                "location_dest": loc_dest,
            }
        )

    # Prepare input data
    input_data = {"rects": rects, "wires": wire_objects, "constraints": constraints}

    try:
        # Run the packing operation in subprocess
        result = subprocess.run(
            [sys.executable, "pcb/ortools_subprocess.py", "pack"],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            cwd=".",
        )

        if result.returncode == 0:
            print(f"Debug - subprocess stdout: '{result.stdout}'")
            print(f"Debug - subprocess stderr: '{result.stderr}'")

            if result.stdout.strip():
                try:
                    # Extract JSON from output (it might have extra lines from ortools)
                    output_lines = result.stdout.strip().split("\n")
                    json_line = None

                    # Find the line that looks like JSON (starts with { and ends with })
                    for line in output_lines:
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            json_line = line
                            break

                    if json_line:
                        response = json.loads(json_line)
                        if response["success"]:
                            print(f"✅ Packing successful: {response['message']}")
                            return response["positions"]
                        else:
                            print(f"❌ Packing failed: {response['error']}")
                            return None
                    else:
                        print("❌ No JSON found in subprocess output")
                        print(f"Raw output: '{result.stdout}'")
                        return None

                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error: {e}")
                    print(f"Raw output: '{result.stdout}'")
                    return None
            else:
                print("❌ Subprocess returned empty output")
                return None
        else:
            print(f"❌ Subprocess failed with return code {result.returncode}")
            print(f"Stderr: {result.stderr}")
            print(f"Stdout: {result.stdout}")
            return None

    except Exception as e:
        print(f"❌ Failed to run packing subprocess: {e}")
        return None


def get_all_connections(pcb_path: Path):
    """Return a list of [ref1, ref2] pairs for every connection between components."""
    board = pcbnew.LoadBoard(pcb_path)

    # Get all footprints
    footprints = list(board.GetFootprints())
    print(f"Found {len(footprints)} footprints")

    # Build nets dictionary: net_name -> list of (ref, pad_name) tuples
    nets = {}
    for fp in footprints:
        for pad in fp.Pads():
            net_name = pad.GetNetname()
            if net_name and net_name != "":
                if net_name not in nets:
                    nets[net_name] = []
                nets[net_name].append((fp.GetReference(), pad.GetName()))

    print(f"Found {len(nets)} nets")

    # Generate all connections (pairs of component references)
    connections = []

    for net_name, pads in nets.items():
        # Skip nets with only one pad (no connections possible)
        if len(pads) < 2:
            continue

        # Generate all pairs of component references in this net
        refs_in_net = [ref for ref, pad_name in pads]
        unique_refs = list(set(refs_in_net))  # Remove duplicates

        # Create pairs between all unique component references
        for i in range(len(unique_refs)):
            for j in range(i + 1, len(unique_refs)):
                connection = [unique_refs[i], unique_refs[j]]
                if (
                    connection not in connections
                    and [connection[1], connection[0]] not in connections
                ):
                    connections.append(connection)
                    print(
                        f"Connection: {connection[0]} <-> {connection[1]} (net: {net_name})"
                    )

    print(f"Total connections found: {len(connections)}")
    return connections


def process_component(
    pcb_path: Path, refs: list[str], all_wires: list[tuple[str, str]]
):

    infos = get_items_infos(pcb_path, refs)

    # Set component name to be the thing with the biggest size
    biggest_size = 0
    component_name = ""

    for info in infos:
        if info.size[0] * info.size[1] > biggest_size:
            biggest_size = info.size[0] * info.size[1]
            component_name = info.name

    # Get sizes
    sizes = [info.size for info in infos]
    component_wires = []

    for wire in all_wires:
        if wire[0] in refs and wire[1] in refs:
            idx1 = refs.index(wire[0])
            idx2 = refs.index(wire[1])

            component_wires.append(
                (
                    idx1,
                    idx2,
                    (sizes[idx1][0] / 2, sizes[idx1][1] / 2),
                    (sizes[idx2][0] / 2, sizes[idx2][1] / 2),
                )
            )

    constraints = [False] * len(sizes)

    print(sizes)
    print(component_wires)
    print(constraints)

    positions = pack_components_via_subprocess(sizes, component_wires, constraints)
    position_map = {ref: pos for ref, pos in zip(refs, positions)}
    final_size = positions.pop()

    print(position_map)
    print(final_size)

    # Generate visualization for this component group
    part_viz = generate_visualization(sizes, positions, component_wires, refs)

    return position_map, component_name, final_size, part_viz


def is_basic_component(identifier: str) -> bool:
    """
    Return True if the identifier looks like a capacitor, resistor,
    crystal, inductor, or diode. Otherwise False.
    """
    if not identifier:
        return False

    s = identifier.upper().strip()

    if s.startswith("R") or s.startswith("R_"):
        return True  # Resistor
    if s.startswith("C") or s.startswith("C_"):
        return True  # Capacitor
    if s.startswith("Y") or "XTAL" in s or "CRYSTAL" in s:
        return True  # Crystal
    if s.startswith("L") or s.startswith("L_"):
        return True  # Inductor
    if s.startswith("D") or s.startswith("D_"):
        return True  # Diode

    return False


def dfs(cur: int, visited: set, adj, comp: list[str]):
    visited.add(cur)
    comp.append(cur)
    for v in adj[cur]:
        if v not in visited and is_basic_component(v):
            dfs(v, visited, adj, comp)


def generate_refs(pcb_path: Path):
    board = pcbnew.LoadBoard(pcb_path)
    wires = get_all_connections(pcb_path)
    all_parts = []
    visited = set()

    adj = defaultdict(list)
    for u, v in wires:
        adj[u].append(v)
        adj[v].append(u)

    for _, fp in enumerate(board.GetFootprints()):
        ref = fp.GetReference()
        all_parts.append(ref)

    refs = []

    for part in all_parts:
        if part not in visited and not is_basic_component(part):
            comp = []
            dfs(part, visited, adj, comp)
            refs.append(comp)

    return refs


def generate_visualization(
    rects: list[tuple[float, float]],
    locations: list[tuple[float, float]],
    wires: list[tuple] | None = None,
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
                fontsize=16,  # Increased from 10 to 16
                fontweight="bold",
                color="black",
                zorder=3,
            )

    # Draw wires if provided
    if wires:
        for wire in wires:
            if wire[0] < len(locations) and wire[1] < len(locations):
                src_pos = locations[wire[0]]
                dest_pos = locations[wire[1]]

                # Calculate wire endpoints
                src_x = src_pos[0] + wire[2][0]
                src_y = src_pos[1] + wire[2][1]
                dest_x = dest_pos[0] + wire[3][0]
                dest_y = dest_pos[1] + wire[3][1]

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


def solve_layout(pcb_path: Path, refs: list[list[str]]) -> list[str]:
    board = pcbnew.LoadBoard(pcb_path)

    all_wires = get_all_connections(pcb_path)
    all_info = []
    part_to_idx = {}
    visualizations = []  # Collect all visualizations

    for i, ref_list in enumerate(refs):
        position_map, component_name, final_size, part_viz = process_component(
            pcb_path, ref_list, all_wires
        )
        all_info.append((position_map, component_name, final_size))
        visualizations.append(part_viz)  # Store part-level visualization

        for part in ref_list:
            part_to_idx[part] = i

        print(position_map)
        print(component_name)
        print(final_size)
        print("--------------------------------")

    overall_sizes = [info[2] for info in all_info]
    overeall_wires = []
    overall_constraints = [False] * len(overall_sizes)

    for wire in all_wires:
        if wire[0] in part_to_idx and wire[1] in part_to_idx:
            idx1 = part_to_idx[wire[0]]
            idx2 = part_to_idx[wire[1]]

            if idx1 == idx2:
                continue
            overeall_wires.append(
                (
                    idx1,
                    idx2,
                    (overall_sizes[idx1][0] / 2, overall_sizes[idx1][1] / 2),
                    (overall_sizes[idx2][0] / 2, overall_sizes[idx2][1] / 2),
                )
            )

    print("Overall:")
    print(overall_sizes)
    print(overeall_wires)
    print(overall_constraints)
    print("--------------------------------")

    big_part_positions = pack_components_via_subprocess(
        overall_sizes, overeall_wires, overall_constraints
    )
    _ = big_part_positions.pop()

    # Place le items
    def get_fp_bbox(fp):
        # KiCad 8/9: GetFootprintRect(); older: GetBoundingBox()
        return get_fp_rect(fp)

    def place_by_bottom_left(fp, target_x_nm, target_y_nm):
        bbox = get_fp_bbox(fp)
        bl = pcbnew.VECTOR2I(
            bbox.GetLeft(), bbox.GetBottom()
        )  # bottom-left in board units (nm)
        # how far we must move the footprint so its BL lands at target
        delta = pcbnew.VECTOR2I(int(target_x_nm - bl.x), int(target_y_nm - bl.y))
        fp.Move(delta)

    # Place part
    for i in range(len(refs)):
        offset = (big_part_positions[i][0], big_part_positions[i][1])
        for part in refs[i]:
            posititon_map = all_info[i][0]
            part_pos = posititon_map[part]  # This is [x, y]
            part_position = (
                part_pos[0] + offset[0],  # x + offset_x
                part_pos[1] + offset[1],  # y + offset_y
            )
            place_by_bottom_left(
                board.FindFootprintByReference(part),
                float(part_position[0]),
                float(part_position[1]),
            )
    pcbnew.SaveBoard(
        "C:\\Users\\alexl\\Documents\\KiCad Projects\\remote-controll\\testing.kicad_pcb",
        board,
    )

    # Generate component-level visualization with component names
    component_names = [info[1] for info in all_info]
    component_viz = generate_visualization(
        overall_sizes, big_part_positions, overeall_wires, component_names
    )
    visualizations.append(component_viz)  # Add final component-level visualization

    return visualizations


def test_packing_subprocess():
    """Test the subprocess packing functionality."""
    print("🧪 Testing subprocess packing...")

    # Simple test case
    rects = [(2.0, 1.0), (1.5, 1.5), (1.0, 2.0)]
    wires_data = [
        (0, 1, (1.0, 0.5), (0.75, 0.75)),  # Connect centers of rect 0 and 1
        (1, 2, (0.75, 0.75), (0.5, 1.0)),  # Connect centers of rect 1 and 2
    ]
    constraints = [False, False, True]  # Only rect 2 must be on edge

    positions = pack_components_via_subprocess(rects, wires_data, constraints)

    if positions:
        print("✅ Packing test successful!")
        print("Positions:", positions)
    else:
        print("❌ Packing test failed!")

    return positions is not None


def display_multiplot(visualizations: list[str], titles: list[str] = None):
    """Display multiple base64 visualizations in a proper NxN grid layout with boxes."""
    n_plots = len(visualizations)

    # Calculate grid dimensions - force square grid
    grid_size = int(n_plots**0.5) + (1 if n_plots**0.5 != int(n_plots**0.5) else 0)
    rows, cols = grid_size, grid_size
    figsize = (6 * cols, 7 * rows)  # Increased height to accommodate titles

    fig, axes = plt.subplots(rows, cols, figsize=figsize, facecolor="white")

    # Handle different subplot configurations
    if n_plots == 1:
        axes = [axes]
    elif rows == 1 or cols == 1:
        axes = axes if hasattr(axes, "__iter__") else [axes]
    else:
        axes = axes.flatten()

    for i, viz_b64 in enumerate(visualizations):
        # Decode base64 image
        img_data = base64.b64decode(viz_b64)
        img = Image.open(io.BytesIO(img_data))

        # Display image
        axes[i].imshow(img)
        axes[i].axis("off")

        # Add a box around each subplot
        axes[i].add_patch(
            plt.Rectangle(
                (0, 0),
                1,
                1,
                transform=axes[i].transAxes,
                fill=False,
                edgecolor="black",
                linewidth=2,
                zorder=10,
            )
        )

        # Add title with better formatting and more padding
        if titles and i < len(titles):
            axes[i].set_title(
                titles[i],
                fontsize=12,
                fontweight="bold",
                pad=25,
                bbox=dict(boxstyle="round,pad=0.5", facecolor="lightblue", alpha=0.8),
            )
        else:
            if i < len(visualizations) - 1:
                axes[i].set_title(
                    f"Component Group {i+1}",
                    fontsize=12,
                    fontweight="bold",
                    pad=25,
                    bbox=dict(
                        boxstyle="round,pad=0.5", facecolor="lightgreen", alpha=0.8
                    ),
                )
            else:
                axes[i].set_title(
                    "Final Layout",
                    fontsize=12,
                    fontweight="bold",
                    pad=25,
                    bbox=dict(
                        boxstyle="round,pad=0.5", facecolor="lightcoral", alpha=0.8
                    ),
                )

    # Hide unused subplots and add empty boxes
    for i in range(n_plots, len(axes)):
        axes[i].axis("off")
        axes[i].set_facecolor("lightgray")
        axes[i].add_patch(
            plt.Rectangle(
                (0, 0),
                1,
                1,
                transform=axes[i].transAxes,
                fill=True,
                facecolor="lightgray",
                edgecolor="gray",
                linewidth=2,
                alpha=0.3,
            )
        )

    # Use subplots_adjust for better control over spacing
    plt.subplots_adjust(
        left=0.05, right=0.95, top=0.85, bottom=0.05, wspace=0.3, hspace=0.4
    )
    plt.show()


if __name__ == "__main__":
    pcb_file = Path(
        "C:\\Users\\alexl\\Documents\\KiCad Projects\\remote-controll\\allm.kicad_pcb"
    )

    refs = generate_refs(pcb_file)
    print(f"Generated {len(refs)} component groups:")
    for i, ref_group in enumerate(refs):
        print(f"  Group {i+1}: {ref_group}")

    # Get all visualizations
    visualizations = solve_layout(pcb_file, refs)

    print(f"\nGenerated {len(visualizations)} visualizations")
    print(f"Part-level visualizations: {len(visualizations)-1}")
    print(f"Component-level visualization: 1")

    # Create titles for the plots
    titles = []
    for i in range(len(visualizations) - 1):
        titles.append(
            f"Group {i+1}: {', '.join(refs[i][:3])}{'...' if len(refs[i]) > 3 else ''}"
        )
    titles.append("Final Component Layout")

    # Display all visualizations
    display_multiplot(visualizations, titles)
