import os
import re
import json
import hashlib
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "data" / "heading_cache.json"


def load_cache():
    """Load local JSON cache of translated headings/slugs."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    """Save local JSON cache of translated headings/slugs."""
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Failed to save heading cache: {e}")



def translate_to_malayalam(text, port, api_key):
    """Translate a short heading from Arabic/English to Malayalam, utilizing offline dict, cache, or local API proxy."""
    dictionary = {
        "[start of document]": "തുടക്കം",
        "start of document": "തുടക്കം",
        "تطير عاب الجزع": "അദ്ദഅ്_വദ്ദവാഅ്",
        "الداء والدواء": "അദ്ദഅ്_വദ്ദവാഅ്",
        "الجواب الكافي": "അദ്ദഅ്_വദ്ദവാഅ്",
        "فصل": "അദ്ധ്യായം",
        "فهارس الكتاب": "സൂചിക",
        "فهرس القوافي": "കവിതാസൂചിക",
        "ولا حول ولا قوة إلا بالله العلي العظيم": "ദുആ",
        "الشرك الأول نوعان": "ശിർക്ക്",
    }
    
    clean_text = re.sub(r'^#+\s*', '', text)
    clean_text = re.sub(r'[\s_\-]+', ' ', clean_text.strip().lower())
    
    tashkeel_re = re.compile(r'[\u064B-\u0652\u0670]')
    clean_text_no_tashkeel = tashkeel_re.sub('', clean_text)
    
    for key, val in dictionary.items():
        if key in clean_text or key in clean_text_no_tashkeel:
            numbers = re.findall(r'\d+', text)
            arabic_numbers = re.findall(r'[\u0660-\u0669]+', text)
            all_numbers = numbers + arabic_numbers
            if val == "അദ്ധ്യായം" and all_numbers:
                arabic_indic = "٠١٢٣٤٥٦٧٨٩"
                digits = "0123456789"
                trans = str.maketrans(arabic_indic, digits)
                num = all_numbers[0].translate(trans)
                return f"{val}_{num}"
            return val
            
    # Try calling the API
    url = f"http://localhost:{port}/v1/chat/completions"
    data = {
        "model": "gemini-3.5-flash-minimal",
        "messages": [
            {
                "role": "system", 
                "content": "Translate the following book section heading into a short, concise Malayalam title (1-4 words). "
                           "Output ONLY the translated Malayalam text, with no extra explanation or punctuation."
            },
            {"role": "user", "content": text}
        ],
        "stream": False
    }
    
    try:
        import urllib.request
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
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.getcode() == 200:
                resp_body = response.read().decode("utf-8")
                result = json.loads(resp_body)
                translated = result["choices"][0]["message"]["content"].strip()
                if translated:
                    return translated
    except Exception:
        pass

    return text


def generate_content_slug(chunk_content, idx, port, api_key):
    """Generate a Malayalam descriptive title from the chunk content using local cache or LLM API."""
    # Use first 1500 characters of the chunk for context (enough to grasp the topic)
    context_len = 1500
    snippet = chunk_content[:context_len].strip()
    
    # Generate a cache key based on a hash of the snippet
    snippet_hash = hashlib.md5(snippet.encode("utf-8")).hexdigest()
    
    # Check cache first
    cache = load_cache()
    if snippet_hash in cache:
        return cache[snippet_hash]
        
    # Build a fallback heading representation if API is offline
    lines = chunk_content.splitlines()
    fallback_title = f"chunk_{idx}"
    for line in lines[:20]:
        line = line.strip()
        if line.startswith("#"):
            fallback_title = line.replace("#", "").strip()
            break
            
    # Try calling the API
    url = f"http://localhost:{port}/v1/chat/completions"
    
    system_instruction = (
        "You are an expert editor. Analyze the provided Arabic text snippet and output a very short "
        "descriptive title (1 to 3 words) in Malayalam script that summarizes the main topic discussed. "
        "Output ONLY the Malayalam script title, no other text, numbers, or punctuation."
    )
    
    data = {
        "model": "gemini-3.5-flash-minimal",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Text Snippet:\n{snippet}"}
        ],
        "stream": False
    }
    
    try:
        import urllib.request
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
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.getcode() == 200:
                resp_body = response.read().decode("utf-8")
                result = json.loads(resp_body)
                translated = result["choices"][0]["message"]["content"].strip()
                if translated:
                    # Save to cache
                    cache[snippet_hash] = translated
                    save_cache(cache)
                    return translated
    except Exception as e:
        print(f"Warning: API call failed for chunk {idx}: {e}. Using fallback title.")
        pass

    # Fallback to translating the first heading
    return translate_to_malayalam(fallback_title, port, api_key)


def slugify_header(header_text):
    """Clean a header text to make it suitable as a safe filename slug, preserving Malayalam and Unicode characters."""
    text = re.sub(r'^#+\s*', '', header_text)
    
    tashkeel_re = re.compile(r'[\u064B-\u0652\u0670]')
    text = tashkeel_re.sub('', text)
    
    text = re.sub(r'[^\w\s\-\u0D00-\u0D7F]', ' ', text)
    text = re.sub(r'[\s_\-]+', '_', text)
    text = text.strip('_')
    
    if len(text) > 50:
        text = text[:50].rstrip('_')
        
    return text


def make_heading_regex(keywords):
    """Build a regex that matches standard markdown headings or lines containing only the custom keywords."""
    escaped_keywords = "|".join(re.escape(k) for k in keywords)
    return re.compile(
        r'^('
        r'\s*#+.*|'  # Markdown headings
        r'\s*#*\s*(?:' + escaped_keywords + r')(?:\s*[\(\/\d\u0660-\u0669\)\].*]*)*\s*'  # Custom keywords
        r')$'
    )



def split_markdown_by_sections(file_path, output_dir, target_words=10000, generic_keywords=None):
    """Split a markdown file into sequential chunks based on natural headings and section boundaries."""
    if generic_keywords is None:
        generic_keywords = ["فصل", "chapter", "section", "part", "باب", "അദ്ധ്യായം", "ഫസൽ"]

    file_path = Path(file_path)
    output_dir = Path(output_dir)

    if not file_path.exists():
        raise FileNotFoundError(f"Input file '{file_path}' does not exist.")

    output_dir.mkdir(parents=True, exist_ok=True)

    port = int(os.environ.get("PORT", 7860))
    api_key = os.environ.get("API_KEY", "123456")

    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    heading_re = make_heading_regex(generic_keywords)

    headings = []
    for idx, line in enumerate(lines):
        line_stripped = line.strip()
        if heading_re.match(line_stripped):
            headings.append((idx + 1, line_stripped))

    sections = []
    
    if not headings or headings[0][0] > 1:
        start_line = 1
        end_line = headings[0][0] - 1 if headings else len(lines)
        section_lines = lines[start_line - 1 : end_line]
        section_text = "\n".join(section_lines)
        sections.append({
            "start_line": start_line,
            "end_line": end_line,
            "header": "[Start of Document]",
            "words": len(section_text.split()),
            "lines": section_lines
        })

    for i, (line_num, text) in enumerate(headings):
        start_line = line_num
        end_line = headings[i+1][0] - 1 if i + 1 < len(headings) else len(lines)
        section_lines = lines[start_line - 1 : end_line]
        section_text = "\n".join(section_lines)
        sections.append({
            "start_line": start_line,
            "end_line": end_line,
            "header": text,
            "words": len(section_text.split()),
            "lines": section_lines
        })

    chunks = []
    current_chunk_sections = []
    current_words = 0

    for sec in sections:
        sec_words = sec["words"]
        
        if current_chunk_sections and current_words + sec_words > target_words:
            curr_diff = abs(target_words - current_words)
            next_diff = abs(target_words - (current_words + sec_words))
            
            if next_diff < curr_diff and (current_words + sec_words) < 1.3 * target_words:
                current_chunk_sections.append(sec)
                current_words += sec_words
                chunks.append(current_chunk_sections)
                current_chunk_sections = []
                current_words = 0
            else:
                chunks.append(current_chunk_sections)
                current_chunk_sections = [sec]
                current_words = sec_words
        else:
            current_chunk_sections.append(sec)
            current_words += sec_words

    if current_chunk_sections:
        chunks.append(current_chunk_sections)

    created_files = []

    for idx, chunk_secs in enumerate(chunks, start=1):
        chunk_lines = []
        for sec in chunk_secs:
            chunk_lines.extend(sec["lines"])
            
        chunk_content = "\n".join(chunk_lines).strip() + "\n"
        
        # Generate a descriptive Malayalam title from the chunk content using LLM
        desc_title = generate_content_slug(chunk_content, idx, port, api_key)
        slug = slugify_header(desc_title)

        if slug:
            chunk_filename = f"{idx:02d}_{slug}.md"
        else:
            chunk_filename = f"{idx:02d}.md"
            
        chunk_path = output_dir / chunk_filename
        chunk_path.write_text(chunk_content, encoding="utf-8")
        
        created_files.append((chunk_path, len(chunk_content.split()), len(chunk_secs)))

    return created_files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split markdown file by logical section headers into balanced chunks.")
    parser.add_argument("--input", "-i", default=str(PROJECT_ROOT / "data" / "output.md"), help="Path to input markdown file")
    parser.add_argument("--output-dir", "-o", default=str(PROJECT_ROOT / "data" / "chunks"), help="Directory to save chunk files")
    parser.add_argument("--target-words", "-w", type=int, default=10000, help="Target word count per chunk")
    parser.add_argument("--heading-keywords", nargs="*", default=["فصل", "chapter", "section", "part", "باب", "അദ്ധ്യായം", "ഫസൽ"], help="Keywords that signify generic headings (like 'Chapter' or 'فصل')")
    args = parser.parse_args()

    try:
        created = split_markdown_by_sections(
            args.input, 
            args.output_dir, 
            target_words=args.target_words,
            generic_keywords=args.heading_keywords
        )
        print(f"Created {len(created)} chunk files in '{args.output_dir}'.")
        for path, words, section_count in created:
            print(f"- {path.name}: {words} words, {section_count} sections")
    except Exception as exc:
        print(f"Error: {exc}")
        raise
