# Configuration

Enable the plugin in your `mkdocs.yml`:

```yaml
markdown_extensions:
  - pymdownx.arithmatex:
      generic: true

plugins:
  - katex-ssr:
      # ... options ...
```

## Basic Options

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `verbose` | bool | `false` | If true, logs the number of formulas, cache hits, and time spent processing each page. |
| `katex_dist` | str | jsDelivr | Base URL for CDN, or local file path to KaTeX distribution. Default: `https://cdn.jsdelivr.net/npm/katex@latest/dist/` |
| `add_katex_css` | bool | `true` | Whether to automatically inject the CSS link tag into the page head. |
| `katex_css_filename` | str | `katex.min.css` | The filename of the CSS file to load. Setting this to `katex-swap.min.css` is recommended for better font loading performance (`font-display: swap`). |
| `katex_options` | dict | `{}` | Standard options passed directly to `katex.renderToString`. Use this to define macros (e.g., `\RR`), set limits, etc. |

## Script Loading (Hybrid Mode)

The plugin distinguishes between scripts needed for **rendering** (SSR) and scripts needed for **interaction** (Client-side).

### Server-Side (`ssr_contribs`)

Scripts listed here are loaded **only in the Node.js renderer**. They affect the generated HTML but are **not** sent to the client browser.

Use this for extensions that modify the output HTML, such as `mhchem`.

```yaml
plugins:
  - katex-ssr:
      ssr_contribs:
        - mhchem
```

### Client-Side (`client_scripts`)

Scripts listed here are injected into the HTML as `<script>` tags. They run in the user's browser.

Use this for interactive features, such as `copy-tex` (clipboard support).

```yaml
plugins:
  - katex-ssr:
      client_scripts:
        - copy-tex
```

## Offline Mode

To create a self-contained documentation site that works without an internet connection, use `embed_assets`.

```yaml
plugins:
  - katex-ssr:
      embed_assets: true
      # Optional: Explicitly point to local node_modules if not auto-detected
      # katex_dist: "./node_modules/katex/dist/"
```

**How it works:**

1. **Copies Assets**: The plugin locates `katex.min.css` (or your configured filename), the `fonts/` folder, and any scripts listed in `client_scripts` from your local `node_modules` directory.
2. **Destinations**: Files are copied to `site/assets/katex/` (configurable via `copy_assets_to`).
3. **Linking**: HTML files are updated to point to these local assets using relative paths (e.g., `../assets/katex/katex.min.css`), ensuring they work even if you open the HTML file directly from your disk.
