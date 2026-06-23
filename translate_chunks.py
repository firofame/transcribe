import os
import sys
import json
import urllib.request
import urllib.error
import concurrent.futures
from threading import Lock
from pathlib import Path

# Paths
TRANSCRIBE_DIR = Path("/Users/firozahmed/Desktop/transcribe")
CHUNKS_DIR = TRANSCRIBE_DIR / "chunks"
TRANSLATED_DIR = TRANSCRIBE_DIR / "translated_chunks"
MASTER_PROMPT_PATH = TRANSCRIBE_DIR / "master_prompt.txt"



def call_translate_api(port, api_key, system_instructions, chunk_content, model):
    url = f"http://localhost:{port}/v1/chat/completions"
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instructions},
            {"role": "user", "content": chunk_content}
        ],
        "stream": False
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
        resp_body = response.read().decode("utf-8")
        if status_code != 200:
            raise RuntimeError(f"HTTP Status Code {status_code}: {resp_body}")
        
        result = json.loads(resp_body)
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected response structure: {resp_body}") from e

print_lock = Lock()

def translate_single_chunk(chunk_file, port, api_key, system_instructions, index, total):
    target_file = TRANSLATED_DIR / chunk_file.name
    
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
    if not MASTER_PROMPT_PATH.exists():
        print(f"Error: Master prompt file not found at {MASTER_PROMPT_PATH}")
        sys.exit(1)

    if not CHUNKS_DIR.exists():
        print(f"Error: Chunks directory not found at {CHUNKS_DIR}")
        sys.exit(1)

    # Ensure output directory exists
    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)

    # Read the master prompt system instructions
    print("Reading master prompt system instructions...")
    system_instructions = MASTER_PROMPT_PATH.read_text(encoding="utf-8")

    # Get all markdown files in the chunks directory and sort them
    chunk_files = sorted([f for f in CHUNKS_DIR.glob("*.md")])
    if not chunk_files:
        print(f"No markdown files found in {CHUNKS_DIR}")
        sys.exit(0)

    total_chunks = len(chunk_files)
    print(f"Found {total_chunks} chunks to process.")

    port = int(os.environ.get("PORT", 7860))
    api_key = os.environ.get("API_KEY", "123456")
    print(f"Connecting to AIStudioToAPI on port {port}...")

    # We use a controlled pool size to prevent hitting API rate limits.
    # Adjust via environment variable TRANSLATE_WORKERS if desired.
    max_workers = int(os.environ.get("TRANSLATE_WORKERS", "3"))
    print(f"Starting parallel translation of {total_chunks} chunks using {max_workers} workers...\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        futures = {
            executor.submit(
                translate_single_chunk, chunk_file, port, api_key, system_instructions, i, total_chunks
            ): chunk_file
            for i, chunk_file in enumerate(chunk_files, start=1)
        }

        all_success = True
        for future in concurrent.futures.as_completed(futures):
            chunk_file = futures[future]
            try:
                success = future.result()
                if not success:
                    all_success = False
                    # Cancel remaining pending tasks
                    for f in futures:
                        f.cancel()
            except Exception as e:
                print(f"Exception raised while translating {chunk_file.name}: {e}", file=sys.stderr)
                all_success = False
                for f in futures:
                    f.cancel()

    if not all_success:
        print("\nError: One or more chunk translations failed. Aborting.", file=sys.stderr)
        sys.exit(1)

    print("\nAll translations completed successfully!")

if __name__ == "__main__":
    main()
