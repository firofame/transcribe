import os
import sys
import json
import argparse
import urllib.request
import urllib.error
from threading import Lock
from pathlib import Path

# Paths
TRANSCRIBE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = TRANSCRIBE_DIR / "data"
MASTER_PROMPT_PATH = TRANSCRIBE_DIR / "prompts" / "master_prompt.txt"



def call_translate_api(port, api_key, system_instructions, chunk_content, model):
    url = f"http://localhost:{port}/v1/chat/completions"
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instructions},
            {"role": "user", "content": chunk_content}
        ],
        "stream": True  # Enable streaming to prevent timeouts
    }
    
    req_body = json.dumps(data).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req, timeout=600) as response:
        status_code = response.getcode()
        if status_code != 200:
            resp_body = response.read().decode("utf-8")
            raise RuntimeError(f"HTTP Status Code {status_code}: {resp_body}")
        
        full_response = []
        for line in response:
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            if line_str.startswith("data: "):
                data_str = line_str[6:]
                if data_str == "[DONE]":
                    break
                try:
                    event_data = json.loads(data_str)
                    choices = event_data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_response.append(content)
                except Exception:
                    pass
        
        return "".join(full_response)

print_lock = Lock()

def translate_single_chunk(chunk_file, port, api_key, system_instructions, index, total, translated_dir):
    target_file = translated_dir / chunk_file.name
    
    # Check if already processed
    if target_file.exists() and target_file.stat().st_size > 0:
        with print_lock:
            print(f"[{index}/{total}] Skipping {chunk_file.name} (already translated).")
        return True

    chunk_content = chunk_file.read_text(encoding="utf-8")
    thinking_levels = ["High", "Medium", "Low", "Minimal"]
    
    for level in thinking_levels:
        model = f"gemini-3.5-flash-{level.lower()}"
        with print_lock:
            print(f"[{index}/{total}] Processing {chunk_file.name} with model: {model}...")

        try:
            response_text = call_translate_api(
                port=port,
                api_key=api_key,
                system_instructions=system_instructions,
                chunk_content=chunk_content,
                model=model
            )
            response_text = response_text.strip()
            if not response_text:
                raise ValueError(f"Empty response returned for {chunk_file.name}")

            # Save translated output
            target_file.write_text(response_text, encoding="utf-8")
            with print_lock:
                print(f"[{index}/{total}] Successfully saved translated output to: {target_file.name}")
            return True

        except urllib.error.HTTPError as e:
            with print_lock:
                print(f"\n[{index}/{total}] [Warning] Failed with Model: {model} (HTTP {e.code})", file=sys.stderr)
                try:
                    err_msg = e.read().decode("utf-8")
                    print(f"Error details: {err_msg}", file=sys.stderr)
                except Exception:
                    print(f"Error: {e.reason}", file=sys.stderr)
                print(f"Retrying with lower thinking level...\n", file=sys.stderr)

        except Exception as e:
            with print_lock:
                print(f"\n[{index}/{total}] [Warning] Failed with Model: {model}", file=sys.stderr)
                print(f"Error details: {e}", file=sys.stderr)
                print(f"Retrying with lower thinking level...\n", file=sys.stderr)

    with print_lock:
        print(f"Error: All thinking levels failed for {chunk_file.name}. Aborting.", file=sys.stderr)
    return False

def main():
    parser = argparse.ArgumentParser(description="Translate markdown chunks in a chapter directory.")
    parser.add_argument(
        "chapter_dir",
        nargs="?",
        default=str(DATA_DIR / "chapters" / "01_supplication_and_cure"),
        help="Path to the chapter directory containing markdown chunks to translate"
    )
    args = parser.parse_args()

    chapter_dir = Path(args.chapter_dir).resolve()
    if not chapter_dir.exists() or not chapter_dir.is_dir():
        print(f"Error: Chapter directory not found or is not a directory: {chapter_dir}", file=sys.stderr)
        sys.exit(1)

    if not MASTER_PROMPT_PATH.exists():
        print(f"Error: Master prompt file not found at {MASTER_PROMPT_PATH}")
        sys.exit(1)

    # Ensure output directory exists
    translated_dir = chapter_dir / "translated"
    translated_dir.mkdir(parents=True, exist_ok=True)

    # Read the master prompt system instructions
    print("Reading master prompt system instructions...")
    system_instructions = MASTER_PROMPT_PATH.read_text(encoding="utf-8")

    # Check for and merge chapter-specific instructions/mappings
    chapter_prompt_path = chapter_dir / "chapter_prompt.txt"
    if chapter_prompt_path.exists():
        print(f"Found chapter-specific instructions/mappings at {chapter_prompt_path.name}. Merging...")
        chapter_instructions = chapter_prompt_path.read_text(encoding="utf-8")
        
        # If <context_translations> section exists in master prompt, inject the chapter-specific corrections cleanly inside it
        if "</context_translations>" in system_instructions:
            parts = system_instructions.split("</context_translations>", 1)
            system_instructions = (
                parts[0]
                + "\n\n<!-- Chapter-Specific Mappings -->\n"
                + chapter_instructions.strip()
                + "\n</context_translations>"
                + parts[1]
            )
        else:
            system_instructions += "\n\n" + chapter_instructions.strip()

    # Get all markdown files in the chapter directory and sort them
    chunk_files = sorted([f for f in chapter_dir.glob("*.md")])
    if not chunk_files:
        print(f"No markdown files found in {chapter_dir}")
        sys.exit(0)

    total_chunks = len(chunk_files)
    print(f"Found {total_chunks} chunks to process in {chapter_dir.name}.")

    port = int(os.environ.get("PORT", 7860))
    api_key = os.environ.get("API_KEY", "123456")
    print(f"Connecting to AIStudioToAPI on port {port}...")

    print(f"Starting sequential translation of {total_chunks} chunks...\n")

    all_success = True
    for i, chunk_file in enumerate(chunk_files, start=1):
        try:
            success = translate_single_chunk(chunk_file, port, api_key, system_instructions, i, total_chunks, translated_dir)
            if not success:
                all_success = False
                break
        except Exception as e:
            print(f"Exception raised while translating {chunk_file.name}: {e}", file=sys.stderr)
            all_success = False
            break

    if not all_success:
        print("\nError: One or more chunk translations failed. Aborting.", file=sys.stderr)
        sys.exit(1)

    print("\nAll translations completed successfully!")

if __name__ == "__main__":
    main()
