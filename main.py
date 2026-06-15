import argparse
import os
from mistralai.client import Mistral

parser = argparse.ArgumentParser(description="Process OCR on a document URL.")
parser.add_argument("document_url", nargs="?", default="https://archive.org/download/UrduBooksCollection_201811/Swila-e-Rahmee.pdf", help="URL of the document to process")
args = parser.parse_args()

api_key = os.environ["MISTRAL_API_KEY"]

client = Mistral(api_key=api_key)

ocr_response = client.ocr.process(
    model="mistral-ocr-latest",
    document={
        "type": "document_url",
        "document_url": args.document_url
    },
    confidence_scores_granularity="page"
)

# Write the OCR response to a JSON file
with open("ocr_response.json", "w", encoding="utf-8") as f:
    f.write(ocr_response.model_dump_json(indent=2))

# Extract and neatly concatenate the markdown from all pages
# ponytail: add support for local image extraction/saving if required by future workflows
pages = sorted(ocr_response.pages, key=lambda p: p.index)
full_markdown = "\n\n---\n\n".join(
    f"<!-- Page {page.index + 1} -->\n{page.markdown}"
    for page in pages
    if page.markdown
)

# Save the concatenated markdown to a file
with open("output.md", "w", encoding="utf-8") as f:
    f.write(full_markdown)

print("OCR complete. Output saved to 'output.md'.")