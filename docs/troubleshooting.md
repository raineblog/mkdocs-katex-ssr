# Troubleshooting

## `katex` module not found

**Error:**

```bash
Error: Cannot find module 'katex'
```

**Solution:**
The Node.js renderer needs to be able to find the `katex` package.

1. Ensure you have run `npm install katex` (or `pnpm`/`yarn`) in the root of your project or in the directory where `mkdocs.yml` is located.
2. If your `node_modules` is in a non-standard location, you can point to it explicitly using `katex_dist`.

## Node.js not found

**Error:**

```bash
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

(or similar `subprocess` errors)

**Solution:**
The plugin requires `node` to be in your system's PATH.

- Open a terminal and run `node --version` to verify it is installed and accessible.
- If running in a CI/CD environment, ensure a "Setup Node" step is included before the MkDocs build.

## Build Warnings

This plugin includes aggressive warning suppression to silence deprecation warnings often emitted by `pkg_resources` and valid but noisy logs from libraries like `jieba`.

If you still see warnings, please verify you are using the latest version of the plugin.
