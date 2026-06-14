# Transcribe

A simple utility script to process document pages using the Mistral OCR API, concatenate the results, and save them as neat Markdown.

## Prerequisites

- Python >= 3.14
- Mistral API Key (set as an environment variable `MISTRAL_API_KEY`)

## Setup

First, install the dependencies. If you are using `uv`:

```bash
uv sync
```

Alternatively, install the required packages:

```bash
pip install .
```

## Usage

Run the script by passing the document URL as an argument:

```bash
python main.py <document_url>
```

If no argument is passed, it defaults to a sample PDF:

```bash
python main.py
```

## Outputs

- `ocr_response.json`: The complete raw JSON output returned by the Mistral OCR API.
- `output.md`: A neatly formatted Markdown document concatenating all document pages, sorted by page index.
