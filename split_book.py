"""Split output.md into 10 roughly equal parts, snapping to <!-- Page N --> boundaries."""
import re, os

INPUT = "output.md"
OUT_DIR = "chapters"
NUM_CHUNKS = 10

with open(INPUT, "r") as f:
    lines = f.readlines()

# Index every page marker: [(line_index, page_number), ...]
page_markers = []
for i, line in enumerate(lines):
    m = re.match(r"<!-- Page (\d+) -->", line.strip())
    if m:
        page_markers.append((i, int(m.group(1))))

# For each target split point, snap to the nearest page marker
target_size = len(lines) / NUM_CHUNKS
split_lines = [0]  # start of first chunk

for chunk_idx in range(1, NUM_CHUNKS):
    target = int(target_size * chunk_idx)
    # Find the page marker whose line is closest to target
    best = min(page_markers, key=lambda pm: abs(pm[0] - target))
    # Use the line *of* the page marker (so the marker starts the new chunk)
    split_lines.append(best[0])

split_lines.append(len(lines))  # end sentinel

os.makedirs(OUT_DIR, exist_ok=True)

for i in range(NUM_CHUNKS):
    start = split_lines[i]
    end = split_lines[i + 1]
    chunk_lines = lines[start:end]

    # Determine page range for the filename
    first_page = last_page = None
    for line in chunk_lines:
        m = re.match(r"<!-- Page (\d+) -->", line.strip())
        if m:
            pg = int(m.group(1))
            if first_page is None:
                first_page = pg
            last_page = pg

    if first_page and last_page:
        fname = f"part_{i+1:02d}_pages_{first_page}-{last_page}.md"
    else:
        fname = f"part_{i+1:02d}.md"

    path = os.path.join(OUT_DIR, fname)
    with open(path, "w") as f:
        f.writelines(chunk_lines)

    print(f"{fname:45s}  lines {start+1:>5}-{end:>5}  ({end-start:>5} lines)")

print(f"\nDone — {NUM_CHUNKS} files written to {OUT_DIR}/")
