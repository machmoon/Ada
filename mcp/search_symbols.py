
import json
from whoosh.fields import Schema, TEXT, ID
from whoosh.index import create_in
from whoosh.qparser import MultifieldParser
import os

SYMBOL_FILE = "symbols.json"
with open(SYMBOL_FILE, 'r', encoding='utf-8') as f:
    SYMBOL_DATA = json.load(f)

# Create Whoosh schema
schema = Schema(
    name=ID(stored=True, unique=True),
    description=TEXT(stored=True),
    ki_keywords=TEXT(stored=True),
    value=TEXT(stored=True),
    footprint=TEXT(stored=True),
)

# Create index in memory (or temp dir)
import tempfile
index_dir = tempfile.mkdtemp()
ix = create_in(index_dir, schema)

# Add documents to index
writer = ix.writer()
for symbol in SYMBOL_DATA:
    writer.add_document(
        name=str(symbol["lib"] + ":" + symbol["properties"]["Value"]),
        description=str(symbol["properties"].get("Description", "")),
        ki_keywords=str(symbol["properties"].get("ki_keywords", "")),
        value=str(symbol["properties"].get("Value", "")),
        footprint=str(symbol["properties"].get("Footprint", "")),
    )
writer.commit()

def search_symbols(query, limit=10):
    """
    Search for symbols matching the query in description, ki_keywords, or value fields.
    Returns a list of matching symbol dicts.
    """
    with ix.searcher() as searcher:
        parser = MultifieldParser(["description", "ki_keywords", "value"], schema=ix.schema)
        q = parser.parse(query)
        results = searcher.search(q, limit=limit)
        matches = []
        for hit in results:
            # Find the original symbol dict by name
            matches.append(hit.fields())
        return matches

if __name__ == "__main__":
    while True:
        query = input("> ")
        results = search_symbols(query)
        print(f"Search results for '{query}':")
        for res in results:
            print(res)