import random
def make_subgraph(prefix, count):
    # Create a subgraph with count nodes, each connected to the next
    return {f"{prefix}{i}": [f"{prefix}{i+1}"] if i+1 < count else [] for i in range(count)}

BUILD_STATE = {
    "status": "idle",
    "components": {},
    "adjGraph": {},
    "layouts": {},
}

# Add 5 hierarchical nodes, each with 8 subnodes
for g in range(5):
    gname = f"graphnode{g+1}"
    subgraph = make_subgraph(f"leaf{g+1}_", 8)
    BUILD_STATE["components"][gname] = {
        "name": f"GraphNode {g+1}",
        "description": f"Hierarchical node {g+1}",
        "img": "/component.jpg",
        "subgraph": subgraph
    }
    BUILD_STATE["adjGraph"][gname] = [f"leaf{g*8+i+1}" for i in range(8)]
    BUILD_STATE["layouts"][gname] = "/layout.png"

# Add 10 simple nodes
for i in range(1, 100):
    lname = f"leaf{i}"
    BUILD_STATE["components"][lname] = {
        "name": f"Leaf {i}",
        "description": f"Simple node {i}",
        "img": "/component.jpg"
    }
    BUILD_STATE["adjGraph"][lname] = []
    BUILD_STATE["layouts"][lname] = "/layout.png"

for i in range(1, 15):
    nname = f"node{i}"
    BUILD_STATE["components"][nname] = {
        "name": f"Node {i}",
        "description": f"Extra node {i}",
        "img": "/component.jpg"
    }
    BUILD_STATE["adjGraph"][nname] = []
    BUILD_STATE["layouts"][nname] = "/layout.png"

# Add 35 more nodes, some connected randomly
for i in range(11, 51):
    nname = f"node{i}"
    BUILD_STATE["components"][nname] = {
        "name": f"Node {i}",
        "description": f"Extra node {i}",
        "img": "/component.jpg"
    }
    # Connect to up to 3 random previous nodes
    prev = [f"node{j}" for j in random.sample(range(1, i), min(3, i-1))] if i > 1 else []
    BUILD_STATE["adjGraph"][nname] = prev
    BUILD_STATE["layouts"][nname] = "/layout.png"

from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route("/buildstatus", methods=["GET"])
def build_status():
    global BUILD_STATE
    return jsonify(BUILD_STATE), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)