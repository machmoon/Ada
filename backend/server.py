from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread
import requests
from bs4 import BeautifulSoup
import ast
import os
import massive
import json
import autoroute
import layout

app = Flask(__name__)
CORS(app)

API_URL = "https://lcsc.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}

AI_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8888/parse")

for chunk in stream:
  print(chunk.choices[0].delta.content or "", end="")

COMPONENTS = []
CONTEXT = ""
# BUILD_STATE = {
#     "status": "idle",
#     "components": {
#         "stm32f103": {
#             "name": "STM32",
#             "description": "Primary microcontroller unit (MCU) for processing and control.",
#             "img": "/component.jpg",
#             "submodules": {
#                 "220uF_capacitor": {
#                     "name": "220uF Capacitor",
#                     "description": "Stabilizes power supply to the MCU.",
#                     "img": "/component.jpg",
#                 },
#                 "10k_resistor": {
#                     "name": "10k Resistor",
#                     "description": "Pull-up resistor for reset pin.",
#                     "img": "/component.jpg",
#                 },
#             },
#         },
#         "drv8825": {
#             "name": "DRV8825",
#             "description": "Step motor driver for controlling stepper motors.",
#             "img": "/component.jpg",
#             "submodules": {
#                 "100uF_capacitor": {
#                     "name": "100uF Capacitor",
#                     "description": "Filters voltage spikes from motor operation.",
#                     "img": "/component.jpg",
#                 },
#                 "1k_resistor": {
#                     "name": "1k Resistor",
#                     "description": "Current limiting resistor for stepper motor coils.",
#                     "img": "/component.jpg",
#                 },
#             },
#         },
#         "graphnode1": {
#             "name": "Subcircuit",
#             "description": "Hierarchical node containing its own graph.",
#             "img": "/component.jpg",
#             "subgraph": {"subA": ["subB", "subC"], "subB": ["subC"], "subC": []},
#         },
#     },
#     "adjGraph": {
#         "stm32f103": ["drv8825"],
#         "r1": ["stm32f103"],
#         "r2": ["stm32f103"],
#         "r3": ["stm32f103"],
#         "r4": ["stm32f103"],
#         "drv8825": ["c1", "c2", "r5"],
#         "c1": [],
#         "c2": [],
#         "r5": [],
#         "graphnode1": ["stm32f103", "drv8825"],
#     },
#     "layouts": {
#         "stm32f103": "/layout.png",
#         "drv8825": "/layout.png",
#         "c1": "/layout.png",
#         "c2": "/layout.png",
#         "graphnode1": "/layout.png",
#     },
# }
BUILD_STATE = {}


def lcsc_search(item):
    search_url = f"{API_URL}/search?q={item}"
    response = requests.get(search_url, headers=HEADERS)
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # get the first result:
    # 1) find the table with className "tableContentTable"
    # 2) find the first row
    # 3) find the second column
    table = soup.find("table", class_="tableContentTable")
    if not table:
        raise ValueError("Table not found!")
    first_row = table.find("tbody").find("tr")
    if not first_row:
        raise ValueError("No results found")
    second_column = first_row.find_all("td")[1]
    # get the link inside the second column
    link = second_column.find("a")["href"]
    print(link)
    product_ID = link[len("https://www.lcsc.com/product-detail/") : len(link) - 5]
    return product_ID


def download_image(product_ID):
    url = f"https://www.lcsc.com/product-image/{product_ID}.html"

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    scripts = soup.find_all("script")

    # find image link
    # find 4th instance of <script>, convert contents into a dict
    contents_dict = ast.literal_eval(scripts[3].string)
    image_link = contents_dict["contentUrl"]
    print(image_link)

    # download the image from the image link
    image_response = requests.get(image_link, headers=HEADERS)
    with open(f"../frontend/silkscreen/public/component_img/{product_ID}.jpg", "wb") as f:
        f.write(image_response.content)
    print("Image", product_ID, "downloaded!")


def run_pipeline(components, context):
    global BUILD_STATE
    BUILD_STATE["status"] = "searching components..."
    names = [lcsc_search(comp) for comp in components if comp.strip() != ""]

    for name in names:
        if os.path.exists("../frontend/silkscreen/public/component_img/" + name + ".jpg"):
            print(f"Image for {name} already exists, skipping download.")
            continue
        download_image(name)

    for i in range(len(names)):
        BUILD_STATE["status"] = f"processing component {i+1}/{len(names)}..."
        if os.path.exists(names[i] + ".json"):
            with open(names[i] + ".json", "r", encoding="utf-8") as f:
                with open("adjacency_" + str(i) + ".json", "w", encoding="utf-8") as f2:
                    adjacency_str = f.read()
                    adjacency = json.loads(adjacency_str)
                    f2.write(adjacency_str)
            aux = adjacency["auxiliary_components"]
            BUILD_STATE["components"][components[i]] = {
                "name": components[i],
                "description": "",
                "img": f"component_img/{names[i]}.jpg",
                "submodules": {
                    c: {
                        "name": f"{aux[c]['value']} {aux[c]['type']}",
                        "description": c,
                        "img": {
                            "resistor": "component_img/C98220.jpg",
                            "capacitor": "component_img/C602037.jpg",
                            "inductor": "component_img/C502009.jpg",
                            "diode": "component_img/C232841.jpg",
                            "crystal": "component_img/C13738.jpg",
                        }[aux[c]["type"]],
                    }
                    for c in aux
                },
            }
            if aux:
                BUILD_STATE["components"][components[i]]["subgraph"] = {
                    c:[] for c in aux
                }
            continue
        else:
            r = requests.post(
                AI_URL
                + "?pdfUrl=https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/"
                + names[i]
                + ".pdf&part_name="
                + names[i],
                timeout=300,
            )
            print("Status:", r.status_code)
            body = r.json()
            structured = body.get("structured")
            with open("adjacency_" + str(i) + ".json", "w", encoding="utf-8") as f:
                f.write(structured)
            with open(names[i] + ".json", "w", encoding="utf-8") as f2:
                f2.write(structured)
            f.close()
            f2.close()
            adjacency = json.loads(structured)
            aux = adjacency["auxiliary_components"]
            BUILD_STATE["components"][components[i]] = {
                "name": components[i],
                "description": "",
                "img": f"/{names[i]}.jpg",
                "submodules": {
                    c: {
                        "name": f"{aux[c]['value']} {aux[c]['type']}",
                        "description": c,
                    }
                    for c in aux
                }
            }

    for (state, complete) in massive.run(BUILD_STATE, len(names), components):
        BUILD_STATE = state

    # for (state, complete) in layout.run(BUILD_STATE, len(names), components):
    #     BUILD_STATE = state
    layout.run()

    autoroute.run()


@app.route("/setquery", methods=["POST"])
def set_query():
    global BUILD_STATE
    print("Received build request")
    data = request.json
    components = data.get("components", [])
    context = data.get("context", "")
    print("Received components:", components)
    print("Received context:", context)

    # launch thread
    thread = Thread(target=run_pipeline, args=(components, context))
    thread.daemon = True
    thread.start()
    BUILD_STATE = {
        "status": "starting pipeline...",
        "components": {},
        "adjGraph": {},
        "layouts": {},
    }
    
    return jsonify({"status": "success", "message": "Query received"}), 200


@app.route("/buildstatus", methods=["GET"])
def build_status():
    global BUILD_STATE
    return jsonify(BUILD_STATE), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
