from kiutils.symbol import SymbolLib
import os
import json

SYMBOL_LIB_PATH = "C:/Program Files/KiCad/8.0/share/kicad/symbols"
SYMBOLS = {}


def load_symbols():
    if not os.path.exists(SYMBOL_LIB_PATH):
        raise FileNotFoundError(f"Symbol library path not found: {SYMBOL_LIB_PATH}")
    for file in os.listdir(SYMBOL_LIB_PATH):
        print(f"Checking file: {file}")
        if file.endswith(".kicad_sym"):
            lib = SymbolLib.from_file(
                os.path.join(SYMBOL_LIB_PATH, file), encoding="utf-8"
            )
            SYMBOLS[file.split(".")[0]] = lib.symbols


def get_props(symbol):
    return {prop.key: prop.value for prop in symbol.properties}


load_symbols()
print(f"Loaded {len(SYMBOLS)} symbols from {SYMBOL_LIB_PATH}")


def generate_symbol_json(filename="symbols.json"):
    ret = []
    for lib_name, symbols in SYMBOLS.items():
        for symbol in symbols:
            props = get_props(symbol)
            ret.append({"lib": lib_name, "properties": props})
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(ret, f, indent=2)


generate_symbol_json()
print(f"Symbol properties written to symbols.json")
