# scripts/fix_trade_memory.py
import json
from pathlib import Path

path = Path("ai/data/trade_memory.jsonl")
if path.exists():
    content = path.read_text(encoding="utf-8-sig")  # Handles BOM
    lines = content.splitlines()
    fixed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Handle case where multiple JSON objects are on one line
        # (e.g., {...}{...} causes "Extra data" error)
        try:
            # Try to parse as single JSON
            json.loads(line)
            fixed.append(line)
        except json.JSONDecodeError:
            # Try to split by '}{' pattern
            if '}{' in line:
                parts = []
                current = ""
                for char in line:
                    current += char
                    if char == '}':
                        try:
                            json.loads(current)
                            parts.append(current)
                            current = ""
                        except:
                            pass
                fixed.extend(parts)
            else:
                print(f"Could not fix line: {line[:100]}...")
    
    path.write_text("\n".join(fixed) + "\n", encoding="utf-8")
    print(f"Fixed {len(fixed)} entries")