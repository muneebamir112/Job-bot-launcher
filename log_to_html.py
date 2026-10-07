import os
import re

def convert_to_html(log_path):
    if not os.path.exists(log_path):
        return

    html_path = log_path.replace('.log', '.html')
    
    with open(log_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    html_lines = []
    html_lines.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobBot Run Log</title>
<style>
    body {
        background-color: #0f1115;
        color: #e6e6e6;
        font-family: 'Consolas', 'Courier New', monospace;
        margin: 0;
        padding: 20px;
        line-height: 1.5;
    }
    .log-container {
        background-color: #171a21;
        border: 1px solid #2a2f3a;
        border-radius: 8px;
        padding: 20px;
        max-width: 1200px;
        margin: 0 auto;
        overflow-x: auto;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    .timestamp { color: #7d8390; }
    .command { color: #3ddc84; font-weight: bold; }
    .header { color: #4f8cff; font-weight: bold; }
    .separator { color: #c77dff; }
    .success { color: #3ddc84; }
    .error { color: #ff5555; font-weight: bold; }
    .url { color: #4f8cff; text-decoration: none; }
    .url:hover { text-decoration: underline; }
    .line { margin: 2px 0; white-space: pre-wrap; word-wrap: break-word; }
</style>
</head>
<body>
<div class="log-container">
""")

    for line in lines:
        line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # Colorize parts of the line
        css_class = ""
        
        # URL linkifying
        line = re.sub(r'(https?://[^\s]+)', r'<a href="\1" class="url" target="_blank">\1</a>', line)
        
        if line.startswith('===') or '===' in line:
            css_class = "header"
        elif line.startswith('---') or '---' in line:
            css_class = "separator"
        elif '$ ' in line:
            css_class = "command"
        elif 'error' in line.lower() or 'failed' in line.lower() or 'exception' in line.lower():
            css_class = "error"
        elif 'done!' in line.lower() or 'saved' in line.lower() or 'success' in line.lower():
            css_class = "success"

        # Highlight timestamp
        match = re.match(r'^(\[[\d\-:\s]+\])(.*)', line)
        if match:
            ts = match.group(1)
            rest = match.group(2)
            html_lines.append(f'<div class="line {css_class}"><span class="timestamp">{ts}</span>{rest}</div>')
        else:
            html_lines.append(f'<div class="line {css_class}">{line}</div>')

    html_lines.append("""</div>
</body>
</html>
""")

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(html_lines))

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        convert_to_html(sys.argv[1])
