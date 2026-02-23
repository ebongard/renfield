import os
import re

def fix_missing_quotes(content):
    lines = content.split('\n')
    new_lines = []
    
    for line in lines:
        # Fix: className="flex space-x-4 role="tablist">
        line = re.sub(r'className="([^"]+?)\s+role=', r'className="\1" role=', line)
        line = re.sub(r'className="([^"]+?)\s+aria-', r'className="\1" aria-', line)
        line = re.sub(r'className="([^"]+?)\s+disabled=', r'className="\1" disabled=', line)
        line = re.sub(r'className="([^"]+?)\s+onClick=', r'className="\1" onClick=', line)
        line = re.sub(r'className="([^"]+?)\s+title=', r'className="\1" title=', line)
        line = re.sub(r'className="([^"]+?)\s+style=', r'className="\1" style=', line)
        line = re.sub(r'className="([^"]+?)\s+rows=', r'className="\1" rows=', line)
        line = re.sub(r'className="([^"]+?)\s+type=', r'className="\1" type=', line)
        line = re.sub(r'className="([^"]+?)\s+value=', r'className="\1" value=', line)
        line = re.sub(r'className="([^"]+?)\s+placeholder=', r'className="\1" placeholder=', line)
        line = re.sub(r'className="([^"]+?)\s+onChange=', r'className="\1" onChange=', line)
        
        # General fix for className ending abruptly before /> or > or {
        line = re.sub(r'className="([^"]+?)(?=\s*\/>)', r'className="\1"', line)
        line = re.sub(r'className="([^"]+?)(?=\s*>)', r'className="\1"', line)
        
        # If the line ends with a className string with no closing quote:
        # e.g.: className="input w-full
        # e.g.: className="card p-6 space-y-6> (with > at the end that should be ">)
        match = re.search(r'className="([^"]+)$', line)
        if match:
            if line.endswith('>'):
                line = line[:-1] + '">'
            else:
                line = line + '"'
        
        # Same for color="..." or size="..." ending at end of line without quote
        match_color = re.search(r'color="([^"]+)$', line)
        if match_color:
            if line.endswith('>'):
                line = line[:-1] + '">'
            else:
                line = line + '"'
                
        # Fix inside JSX expressions with missing quotes like <Icon className="w-4 h-4 />
        line = re.sub(r'className="([^"]+?)\s+\/>', r'className="\1" />', line)
        
        new_lines.append(line)
        
    return '\n'.join(new_lines)

def process_directory(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.jsx'):
                filepath = os.path.join(root, file)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                fixed_content = fix_missing_quotes(content)
                if content != fixed_content:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(fixed_content)
                    print(f"Fixed quotes in {file}")

if __name__ == "__main__":
    process_directory('src/pages')
