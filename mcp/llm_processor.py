"""
llm_processor.py
Integrated Claude Sonnet 4 processor for MCP project.
"""

import os
import json
import requests
import re
import anthropic

# ===== CONFIG =====
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
API_URL = "https://api.anthropic.com/v1/messages"

if not CLAUDE_API_KEY:
    raise RuntimeError("CLAUDE_API_KEY environment variable is not set!")

# ===== PROMPT TEMPLATE =====
SYSTEM_PROMPT = (
    """
    You are an expert electrical engineer AI with specific expertise in PCB components. You will be given:

    1. **A PCB component name** (e.g., "ATmega328P", "LM7805").
    2. **A PDF datasheet** for this component.

    Your task is to integrate this chip:

    What auxiliary connections and components do I need? Return result as a JSON in the schema {<component_name>: ["type":<component type>, "value":<component value]}. Furthermore, provide a JSON that describes all connections between components.

    **Example output:**

    ```json
    {
        "auxiliary_components": {
            "decoupling_capacitor_avdd": {
                "type": "capacitor",
                "value": "0.1µF"
            },
            "decoupling_capacitor_dvdd": {
                "type": "capacitor", 
                "value": "0.1µF"
            }
        },
        "device_pins": {
            "AVDD": "device_pin_1",
            "AVSS": "device_pin_2",
            "DVDD": "device_pin_3",
            "DGND": "device_pin_4"
        },
        "connections": {
            "power_supply": {
                "AVDD": ["decoupling_capacitor_avdd", "AIN0/REFP1", "AIN1", "AIN2", "AIN3/REFN1"],
                "decoupling_capacitor_avdd": ["AVDD", "AVSS"],
                "DVDD": ["decoupling_capacitor_dvdd", "digital_interface_pins"],
                "decoupling_capacitor_dvdd": ["DVDD", "DGND"]
            },
            "analog_inputs": {
                "AIN0/REFP1": ["sensor_input"],
                "AIN1": ["sensor_input"],
                "AIN2": ["sensor_input"],
                "AIN3/REFN1": ["sensor_input"]
            },
            "digital_interface": {
                "SCLK": ["microcontroller_spi"],
                "CS": ["microcontroller_spi"],
                "DIN": ["microcontroller_spi"],
                "DOUT/DRDY": ["microcontroller_spi"],
                "DRDY": ["microcontroller_interrupt"]
            },
            "reference_inputs": {
                "REFP0": ["external_reference"],
                "REFN0": ["external_reference"]
            },
            "ground_connections": {
                "AVSS": ["analog_ground"],
                "DGND": ["digital_ground"]
            }
        }
    }
    ```

    **Rules:**

    * Only include **required connections for basic functional operation**.
    * Each edge represents a **necessary electrical connection**.
    * SUPER IMPORTANT: Only use simple components. Do **NOT** use components like "analog_input_filter". Formally, stick to components that will be found in KiCad's library.
    * Any complicated parts NOT in KiCad's library returned will be useless. If you are unsure if something is too complex, it probably is.
    * If a component is too complex, boil it down to its simpler components and describe the connections between them.
    * For auxiliary components, you may ONLY consider the following prizes, but make sure you have all the REQUIRED connections:
        - capacitor
        - resistor
        - crystal
        - inductor
        - diode
    * Make sure all of these components, if used in the edges, are also present in the auxiliary components.
    * ***CRITICAL*** DO NOT MISS THIS STEP! Define what "device_pin_i" is for all nodes that are not auxiliary components in connections.
        - !IMPORTANT! Do not define it as "pin_1", define it as "device_pin_1".
    * Make sure the "device_pin_i" is consistent and a valid pin number for the central device.
    * Make sure your pin numbers range ONLY from **1-32**, inclusive.

    IMPORTANT: Do **not** include any explanations, reasoning, or extra text.  
    Output **only** a valid JSON adjacency list exactly as shown in the example.  
    The JSON must be parseable; do not include comments, markdown, or extra formatting.
    """
)

# ===== CORE CLAUDE CALL =====
def parse_datasheet_with_llm(pdf_url, part_name):
    # headers = {
    #     "x-api-key": CLAUDE_API_KEY,
    #     "Anthropic-Version": "2023-06-01",
    #     "Content-Type": "application/json"
    # }
    # payload = {
    #     "model": CLAUDE_MODEL,
    #     "max_tokens": 1024,
    #     "messages": messages
    # }
    import base64
    # with open("./mcp/datasheet.pdf", "rb") as f:
    #     pdf_bytes = f.read()
    # pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=1024,
        messages = [
            {"role": "user", "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "url",
                        "url": pdf_url
                    }
                },
                # {
                #     "type": "document",
                #     "source": {
                #         "type": "base64",
                #         "media_type": "application/pdf",
                #         "data": pdf_b64
                #     }
                # },
                {"type": "text", "text": SYSTEM_PROMPT + "\n\n" + part_name}]}]
    )
    # resp = requests.post(API_URL, headers=headers, json=payload, timeout=300)
    # resp.raise_for_status()
    return message.content[0].text
