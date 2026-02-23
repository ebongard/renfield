import os
import re
import glob

def fix_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Regex to find className="... that misses a closing quote
    # It looks for className=" followed by characters (including tailwind specific)
    # until it hits /> or > or \n or another attribute.
    # We use a negative lookahead to ensure we don't match if there's already a closing quote.
    
    # Pattern explanation:
    # className=" : Literal start
    # ([a-zA-Z0-9\-_:\/\[\]\.%\s]+?) : Capture the class names
    # (?=\s*(?:/>|>|\n|aria-|role=|title=|data-|style=|onClick=|id=|type=|href=|disabled=|onChange=|\{))
    # We only replace if the matched string doesn't end with "
    
    # Wait, simple approach: split by `className="`
    parts = content.split('className="')
    if len(parts) == 1:
        return False
        
    new_content = parts[0]
    for part in parts[1:]:
        # Find the end of the class string. It ends right before a newline, />, >, or a known attribute, or "
        # Let's read char by char until we hit something that indicates the end of the attribute.
        idx = 0
        in_bracket = False
        while idx < len(part):
            c = part[idx]
            if c == '[':
                in_bracket = True
            elif c == ']':
                in_bracket = False
            elif c == '"' and not in_bracket:
                # Found the closing quote normally
                break
            elif c == '\n':
                # Missing quote before newline
                break
            elif c == '>' and not in_bracket:
                # Missing quote before >
                # Check previous chars for />
                if idx > 0 and part[idx-1] == '-': # wait, />
                    pass
                break
            elif c == '=' and not in_bracket:
                # We hit an equal sign. This means we entered another attribute!
                # E.g. role=
                # So the class ended before the word ending in =
                break
            idx += 1
            
        # If we hit an = sign, we need to backtrack to the space before the attribute name
        if idx < len(part) and part[idx] == '=':
            # backtrack to space
            while idx > 0 and part[idx] != ' ':
                idx -= 1
        
        # If we hit >, we should check if it's />
        if idx < len(part) and part[idx] == '>':
            if idx > 0 and part[idx-1] == '/':
                idx -= 1
                
        # Now idx is the position where the closing quote should be.
        class_str = part[:idx]
        remainder = part[idx:]
        
        # Strip trailing spaces from class_str
        class_str_stripped = class_str.rstrip()
        
        # Calculate how many spaces were removed
        spaces_removed = len(class_str) - len(class_str_stripped)
        
        # If the original string had a quote, class_str_stripped will be empty if it started with quote... wait.
        # If it ended with quote naturally:
        if len(class_str) > 0 and class_str[-1] == '"':
            new_content += 'className="' + class_str + remainder
        elif len(remainder) > 0 and remainder[0] == '"':
            new_content += 'className="' + part
        else:
            # We found a missing quote!
            # Format: 'className="' + class_str_stripped + '"' + spaces + remainder
            spaces = ' ' * spaces_removed
            new_content += 'className="' + class_str_stripped + '"' + spaces + remainder
            
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    return False

if __name__ == "__main__":
    files = glob.glob('src/frontend/src/pages/*.jsx') + glob.glob('src/frontend/src/components/*.jsx')
    fixed_count = 0
    for file in files:
        if fix_file(file):
            print(f"Fixed quotes in {file}")
            fixed_count += 1
    print(f"Total files fixed: {fixed_count}")
