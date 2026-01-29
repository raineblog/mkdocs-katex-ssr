const katex = require('katex');
const readline = require('readline');

// Store loaded contrib names to avoid reloading
const loadedContribs = new Set();

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
});

rl.on('line', (line) => {
    if (!line.trim()) return;
    
    try {
        const data = JSON.parse(line);
        
        if (data.type === 'setup') {
            if (data.contribs && Array.isArray(data.contribs)) {
                data.contribs.forEach(name => {
                    if (!loadedContribs.has(name)) {
                        try {
                            // Try to load from katex/dist/contrib/
                            require(`katex/dist/contrib/${name}.js`);
                            loadedContribs.add(name);
                        } catch (e) {
                            // Also try without .js or direct name if it's external
                            try {
                                require(name);
                                loadedContribs.add(name);
                            } catch (e2) {
                                console.error(`Failed to load contrib: ${name}`);
                            }
                        }
                    }
                });
            }
            return; // No response needed for setup
        }

        if (data.type === 'render' || !data.type) {
            const { latex, displayMode, options } = data;
            
            const html = katex.renderToString(latex, {
                displayMode: displayMode || false,
                throwOnError: false,
                ...options
            });
            
            process.stdout.write(JSON.stringify({ status: 'success', html }) + '\n');
        }
    } catch (err) {
        process.stdout.write(JSON.stringify({ status: 'error', message: err.message }) + '\n');
    }
});

console.error('KaTeX SSR Renderer started');
