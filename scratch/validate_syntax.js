const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('BusyMobileWeb/index.html', 'utf-8');
const scriptMatches = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];

console.log(`Found ${scriptMatches.length} script blocks.`);
let hasError = false;

scriptMatches.forEach((m, idx) => {
    const code = m[1];
    // Skip external script src tags
    if (!code.trim()) return;
    try {
        new vm.Script(code);
        console.log(`Script block ${idx + 1} syntax: VALID`);
    } catch (e) {
        hasError = true;
        console.error(`Script block ${idx + 1} SYNTAX ERROR:`, e);
    }
});

if (!hasError) {
    console.log("All inline scripts have 100% valid JavaScript syntax!");
}

