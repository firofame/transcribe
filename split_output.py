import os
import re
import argparse
from pathlib import Path


def split_markdown_by_pages(file_path, output_dir, target_words=10000, page_pattern=r'<!-- Page (\d+) -->'):
    """Split a markdown file into sequential chunks by page markers and target word count."""
    file_path = Path(file_path)
    output_dir = Path(output_dir)

    if not file_path.exists():
        raise FileNotFoundError(f"Input file '{file_path}' does not exist.")

    output_dir.mkdir(parents=True, exist_ok=True)

    content = file_path.read_text(encoding="utf-8")
    page_regex = re.compile(page_pattern)
    page_matches = list(page_regex.finditer(content))

    if not page_matches:
        raise ValueError(f"No page markers found matching the pattern: {page_pattern}")

    pages = []
    for i, match in enumerate(page_matches):
        start_pos = match.start()
        end_pos = page_matches[i + 1].start() if i + 1 < len(page_matches) else len(content)
        page_num = int(match.group(1))
        page_text = content[start_pos:end_pos].strip() + "\n"
        words = len(page_text.split())

        pages.append({
            "num": page_num,
            "text": page_text,
            "words": words,
        })

    chunks = []
    current_chunk = []
    current_words = 0

    for page in pages:
        if current_chunk and current_words + page["words"] > target_words:
            chunks.append(current_chunk)
            current_chunk = [page]
            current_words = page["words"]
        else:
            current_chunk.append(page)
            current_words += page["words"]

    if current_chunk:
        chunks.append(current_chunk)

    created_files = []
    for idx, chunk_pages in enumerate(chunks, start=1):
        start_pg = chunk_pages[0]["num"]
        end_pg = chunk_pages[-1]["num"]
        chunk_content = "\n\n".join(p["text"].strip() for p in chunk_pages).strip() + "\n"
        chunk_filename = f"chunk_{idx:03d}_pages_{start_pg}_to_{end_pg}.md"
        chunk_path = output_dir / chunk_filename
        chunk_path.write_text(chunk_content, encoding="utf-8")
        created_files.append((chunk_path, len(chunk_content.split()), len(chunk_pages)))

    return created_files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split markdown file by page markers into balanced chunks.")
    parser.add_argument("--input", "-i", default="output.md", help="Path to input markdown file")
    parser.add_argument("--output-dir", "-o", default="chunks", help="Directory to save chunk files")
    parser.add_argument("--target-words", "-w", type=int, default=10000, help="Target word count per chunk")
    parser.add_argument("--pattern", "-p", default=r'<!-- Page (\d+) -->', help="Regex pattern for page markers")
    args = parser.parse_args()

    try:
        created = split_markdown_by_pages(args.input, args.output_dir, target_words=args.target_words, page_pattern=args.pattern)
        print(f"Created {len(created)} chunk files in '{args.output_dir}'.")
        for path, words, page_count in created:
            print(f"- {path.name}: {words} words, {page_count} pages")
    except Exception as exc:
        print(f"Error: {exc}")
        raise
