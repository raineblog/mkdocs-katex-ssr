# Installation

## Prerequisites

- **Bun** or **Node.js**: Must be installed and available in your system PATH (`bun` is recommended for optimal speed).
- **Python**: 3.8+

## Install Plugin

You can install the plugin directly from PyPI:

```bash
pip install mkdocs-katex-ssr
```

Alternatively, if you are installing from source:

```bash
git clone https://github.com/raineblog/mkdocs-katex-ssr.git
cd mkdocs-katex-ssr
```bash
pip install .
```

## Basic Configuration

Enable the plugin in your `mkdocs.yml`:

```yaml
plugins:
  - katex-ssr
```

For more advanced options, including how to **disable SSR** for client-side only rendering, see the [Configuration](configuration.md) page.
