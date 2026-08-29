import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

_PROMPT = """
You are an expert in PCB layout design. You are given a list of components for a layout
and need to determine which components **must** be on the edge. For example, USBs or things with
antennas need to be on the edge.

Output format:
Always return a list of Python booleans, one for each component and
NOTHING ELSE.

Example: [True, False, True, False, False, True, False, False]

Input:
{part_names}
"""


def get_constraints(part_names: list[str]) -> list[bool]:
    client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    for _ in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[
                {"role": "user", "content": _PROMPT.format(part_names=part_names)}
            ],
        )
        try:
            return list(map(bool, eval(response.content[0].text)))
        except Exception as e:
            print(f"Error: {e}; Trying again...")
            continue
    raise RuntimeError("Failed to get constraints")


if __name__ == "__main__":
    print(
        get_constraints(["USB", "Antenna", "IC", "Connector", "Power Module", "Sensor"])
    )
