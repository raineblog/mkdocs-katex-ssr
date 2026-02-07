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

If you still see warnings, please verify you are using the latest version of the plugin.

## Configuration Validation Error

**Error:**

```text
Config value 'katex-ssr': When 'disable' is true, 'add_katex_css' must also be true...
```

**Solution:**

When you set `disable: true` to use client-side rendering, the plugin must be allowed to inject the KaTeX CSS. Ensure `add_katex_css` is not set to `false`.

```yaml
plugins:
  - katex-ssr:
      disable: true
      add_katex_css: true # This must be true
```
