# Document Splitting & Translation Guidelines

This workspace contains configurations and guidelines for translating large OCR-generated documents (such as books) into translated prose optimized for Text-to-Speech (TTS) narration.

---

## 1. Document Splitting Logic

### Rationale
* **LLM Output Limits:** Large language models (e.g., Gemini 3.5 Flash) have strict output token limits (typically **65,536 tokens**).
* **Translation Expansion:** Translating text (especially to agglutinative languages or when including phonetic transliterations) increases the output word and token count substantially.
* **Target Chunk Size:** To prevent generation truncation, source files must be partitioned into chunks targeting **~8,000 - 10,000 source words**.
* **Page-Level Consistency:** Splitting should align with page boundaries (e.g., `<!-- Page X -->` markers) to keep structural context intact and make chunk tracking easy.
* **Index Omission:** Index pages or appendices (like subject, verse, or location indexes) that reference page numbers should be omitted from translation since page numbers do not map to the final translation, and lists of page numbers are not useful for audio/TTS narration.

---

## 2. Generic Splitting Script

Use the script below to split any markdown file with page comment markers into balanced chunks based on a target word count:

```python
import os
import re
import argparse

def split_markdown_by_pages(file_path, output_dir, target_words=9000, page_pattern=r'<!-- Page (\d+) -->'):
    """
    Splits a markdown file into sequential chunks based on page comments,
    grouping pages together to approximate a target word count per chunk.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find page boundaries
    page_regex = re.compile(page_pattern)
    page_matches = list(page_regex.finditer(content))

    if not page_matches:
        print("Error: No page markers found matching the pattern.")
        return

    # Extract positions and page info
    pages = []
    for i in range(len(page_matches)):
        start_pos = page_matches[i].start()
        pg_num = page_matches[i].group(1)
        end_pos = page_matches[i+1].start() if i + 1 < len(page_matches) else len(content)
        
        page_text = content[start_pos:end_pos]
        words = len(page_text.split())
        
        pages.append({
            'num': pg_num,
            'text': page_text,
            'words': words
        })

    # Group pages into chunks
    chunks = []
    current_chunk = []
    current_words = 0

    for page in pages:
        # If adding this page exceeds target and we already have pages in the chunk, finalize it
        if current_chunk and current_words + page['words'] > target_words:
            chunks.append(current_chunk)
            current_chunk = [page]
            current_words = page['words']
        else:
            current_chunk.append(page)
            current_words += page['words']

    if current_chunk:
        chunks.append(current_chunk)

    # Write the chunks to the output directory
    for idx, chunk_pages in enumerate(chunks, 1):
        start_pg = chunk_pages[0]['num']
        end_pg = chunk_pages[-1]['num']
        
        chunk_content = "".join(p['text'] for p in chunk_pages).strip()
        chunk_filename = f"Pages_{start_pg}_to_{end_pg}.md"
        chunk_filepath = os.path.join(output_dir, chunk_filename)
        
        with open(chunk_filepath, "w", encoding="utf-8") as out_f:
            out_f.write(chunk_content)
            
        print(f"Created: {chunk_filename} ({len(chunk_content.split())} words)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split markdown file by page markers.")
    parser.add_argument("--input", "-i", default="output.md", help="Path to input markdown file")
    parser.add_argument("--output-dir", "-o", default="splits", help="Directory to save splits")
    parser.add_argument("--target-words", "-w", type=int, default=9500, help="Target word count per chunk")
    parser.add_argument("--pattern", "-p", default=r'<!-- Page (\d+) -->', help="Regex pattern for page markers")
    args = parser.parse_args()
    
    if os.path.exists(args.input):
        print(f"Splitting {args.input} into chunks of ~{args.target_words} words...")
        split_markdown_by_pages(args.input, args.output_dir, target_words=args.target_words, page_pattern=args.pattern)
    else:
        print(f"Input file '{args.input}' not found. Please adjust paths.")
```

---

## 3. General Translation Instructions for Agents

When processing split chunks:
1. **TTS Formatting Rules:**
   - **Pure Target Unicode:** The output must contain only characters belonging to the target language Unicode block, whitespace, and standard punctuation. Never output foreign characters.
     - *Homoglyph / Visual Illusion Warning:* Ensure the LLM does not generate visually similar homoglyphs from other blocks (e.g., Thai characters `ล`, `่`, `า`, `น` mimicking Malayalam `ലാ`, `ന`, `ാ`, or Arabic script in scholar names like `معروف` and English characters like `u` in place of vowel sign `ു`). Use a script to validate character ranges (e.g., U+0D00 to U+0D7F, allowing U+200C / ZWNJ, standard punctuation, and whitespace).
   - **Zero Brackets:** Do not output brackets, parentheses, or braces of any kind. Parenthetical explanations or translations must be woven naturally into prose.
   - **Zero Numerals:** All digits and years must be written out fully in the target language's words.
   - **No Markdown Layouts:** Do not output page dividers, lists, or headers. Write paragraphs separated by double newlines to guide natural speech pauses.
   - **Omit Footnotes:** All footnotes, critical apparatus, and citations must be completely removed from the translation stream.
2. **Translate Chunks Sequentially:** Process the generated files (e.g. 01_chunk.md, 02_chunk.md, etc.) in order to maintain narrative and contextual continuity.

---

## 4. Google AI Studio Browser Automation

To automate the submission of system instructions, chunk files, and prompts to Google AI Studio, use the browser automation script located in the `aistudio` workspace.

### Prerequisites
1. Start Microsoft Edge or Google Chrome with remote debugging enabled:
   ```bash
   microsoft-edge --remote-debugging-port=9222
   ```
2. Log into [Google AI Studio](https://aistudio.google.com/) in the browser session.

### Execution Command
Run the script to automatically configure system instructions, upload the chunk file, and submit the prompt:
```bash
python3 main.py \
  --system-instructions "$(cat /path/to/master_prompt.txt)" \
  --upload /path/to/splits/[chunk_file].md \
  --prompt "Please process the uploaded file."
```

### Options
* `--system-instructions`: Text to load into the system instructions panel.
* `--upload`, `-u`: Path to the local file to upload into the prompt container.
* `--prompt`, `-p`: User prompt text to submit. If omitted, the script only configures options and uploads the file without running.
* `--thinking-level`: Configures the thinking level (Minimal, Low, Medium, High).
  - *Safety/Prohibited Content Filter Workaround:* When processing texts with graphic descriptions (e.g., hellfire, punishment details) that trigger safety blockages on `High` thinking level, use `--thinking-level Medium` or `Low` to complete the translation successfully.
* `--timeout`, `-t`: Maximum time in seconds to wait for model response stream (default: 600).
* `--port`: Browser remote debugging port (default: 9222).
* `--new-chat`: Opens a new chat tab prior to running commands.

---

## 5. Text-to-Speech (TTS) Execution

Use the Google Docs TTS integration tool located in the `google-docs-tts` workspace to convert the translated chunks into `.ogg` audio files and send them over WhatsApp.

### Execution Command
Run the CLI tool using `uv` inside the `google-docs-tts` folder:
```bash
cd /home/firoz/Desktop/google-docs-tts
uv run tts.py "/home/firoz/Desktop/transcribe/splits/[translated_file].md"
```

### Flow Details
1. **Google Docs Sync:** The script writes text chunks to Google Docs.
2. **Audio Generation:** Triggers the native Google Docs TTS via browser automation to synthesize high-quality audio.
3. **Download & Concatenation:** Merges all processed parts into a single `.ogg` file (saved in the same directory as the input file).
4. **WhatsApp Delivery:** Automatically splits files exceeding the 16 MB WhatsApp limit and delivers them as voice notes to the recipient.

