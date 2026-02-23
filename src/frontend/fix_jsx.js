const fs = require('fs');
const path = require('path');

function walkDir(dir, callback) {
    fs.readdirSync(dir).forEach(f => {
        let dp = path.join(dir, f);
        if (fs.statSync(dp).isDirectory()) walkDir(dp, callback);
        else callback(dp);
    });
}

function fixMissingQuotes(content) {
    let lines = content.split('\n');
    let newLines = [];

    for (let i = 0; i < lines.length; i++) {
        let line = lines[i];

        // Fix className="... />
        line = line.replace(/([a-zA-Z\-]+)="([^"]+?)\s*\/>/g, '$1="$2" />');

        // Fix className="... >
        line = line.replace(/([a-zA-Z\-]+)="([^"]+?)\s*>/g, '$1="$2">');

        // Fix className="... {
        line = line.replace(/([a-zA-Z\-]+)="([^"]+?)\s*(disabled=|aria-|role=|title=|data-|style=|onClick=|id=|type=|href=|onChange=|rows=|maxLength=|value=)/g, '$1="$2" $3');

        // Fix className="... at end of line
        // If line has className="... but no closing quote before the end of the line
        // AND the next line has attributes or /> or >
        let match = line.match(/([a-zA-Z\-]+)="([^"]*)$/);
        if (match) {
            line = line + '"';
        }

        // Some specific cases like: role="status aria-label={...}
        line = line.replace(/role="([^"]+?)\s+(aria-[a-zA-Z\-]+={)/g, 'role="$1" $2');

        newLines.push(line);
    }

    return newLines.join('\n');
}

function processFile(filePath) {
    if (!filePath.endsWith('.jsx')) return;
    let content = fs.readFileSync(filePath, 'utf8');
    let fixed = fixMissingQuotes(content);

    // Also replace old UI with new UI where possible? No, we just fix quotes.
    if (content !== fixed) {
        fs.writeFileSync(filePath, fixed, 'utf8');
        console.log("Fixed quotes in " + filePath);
    }
}

walkDir('src/pages', processFile);
