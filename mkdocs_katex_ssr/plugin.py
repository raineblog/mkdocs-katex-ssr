import os
import json
import sqlite3
import hashlib
import subprocess
import threading
import warnings
import logging
import shutil
from mkdocs.plugins import BasePlugin
from mkdocs.config import config_options
from mkdocs.utils import get_relative_url
from bs4 import BeautifulSoup

# Extremely aggressive global warning suppression
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

class WarningFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage().lower()
        if "pkg_resources" in msg or "jieba" in msg or "deprecationwarning" in msg or "userwarning" in msg:
            return False
        return True

# Apply filter to all potential loggers
for logger_name in ["mkdocs", "mkdocs.plugins", "py.warnings", ""]:
    logger = logging.getLogger(logger_name)
    logger.addFilter(WarningFilter())

logging.captureWarnings(True)

class KatexSsrPlugin(BasePlugin):
    config_scheme = (
        ('katex_dist', config_options.Type(str, default='https://cdn.jsdelivr.net/npm/katex@latest/dist/')),
        ('katex_css_filename', config_options.Type(str, default='katex.min.css')),
        ('add_katex_css', config_options.Type(bool, default=True)),
        ('embed_assets', config_options.Type(bool, default=False)),
        ('copy_assets_to', config_options.Type(str, default='assets/katex')),
        ('ssr_contribs', config_options.Type(list, default=[])),
        ('client_scripts', config_options.Type(list, default=[])),
        # Legacy/Alias for ssr_contribs to maintain some compat, though behavior changes
        ('contrib_scripts', config_options.Type(list, default=[])), 
        ('katex_options', config_options.Type(dict, default={})),
    )

    def __init__(self):
        self.process = None
        self.lock = threading.Lock()
        self._asset_cache = {}
        self._local_dist_path = None
        self.db_conn = None
        self.db_path = None

    def _ensure_trailing_slash(self, path):
        if not path.endswith('/') and not path.endswith('\\'):
            return path + '/'
        return path

    def _resolve_url(self, base, path):
        base = base.replace('\\', '/')
        if base.startswith('http'):
            return base.rstrip('/') + '/' + path.lstrip('/')
        else:
            return os.path.normpath(os.path.join(base, path))

    def on_config(self, config):
        self.config['katex_dist'] = self._ensure_trailing_slash(self.config['katex_dist'])
        
        project_dir = os.path.dirname(config['config_file_path'])
        
        # Initialize Cache DB
        try:
            cache_dir = os.path.join(project_dir, '.cache', 'plugin', 'katex-ssr')
            os.makedirs(cache_dir, exist_ok=True)
            self.db_path = os.path.join(cache_dir, 'cache.db')
            self.db_conn = sqlite3.connect(
                self.db_path, 
                check_same_thread=False
            )
            with self.db_conn:
                self.db_conn.execute('CREATE TABLE IF NOT EXISTS katex_cache (hash TEXT PRIMARY KEY, html TEXT)')
        except Exception as e:
            print(f"Warning: Failed to initialize KaTeX SSR cache: {e}")
            self.db_conn = None
        
        # Merge legacy contrib_scripts into ssr_contribs if used
        if self.config['contrib_scripts']:
            # Append unique items
            for script in self.config['contrib_scripts']:
                if script not in self.config['ssr_contribs']:
                    self.config['ssr_contribs'].append(script)

        # Asset resolution logic
        possible_dist = self._resolve_url(project_dir, self.config['katex_dist'])
        if os.path.isdir(possible_dist):
             self._local_dist_path = possible_dist
        else:
             node_modules = os.path.join(project_dir, 'node_modules')
             dist = os.path.join(node_modules, 'katex', 'dist')
             if os.path.isdir(dist):
                 self._local_dist_path = dist

        # Start Node.js process
        renderer_path = os.path.join(os.path.dirname(__file__), 'renderer.js')
        
        use_shell = os.name == 'nt'
        cmd = ['node', renderer_path]
        if use_shell:
             cmd = f'node "{renderer_path}"'
        
        try:
            env = os.environ.copy()
            node_modules = os.path.join(project_dir, 'node_modules')
            if 'NODE_PATH' in env:
                env['NODE_PATH'] = node_modules + os.pathsep + env['NODE_PATH']
            else:
                env['NODE_PATH'] = node_modules

            self.process = subprocess.Popen(
                cmd,
                cwd=project_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=use_shell,
                env=env
            )
            
            # Send ONLY ssr_contribs to Node
            node_contribs = [c for c in self.config['ssr_contribs'] if '://' not in c]
            setup_payload = {
                'type': 'setup',
                'contribs': node_contribs
            }
            try:
                line = (json.dumps(setup_payload) + '\n').encode('utf-8')
                self.process.stdin.write(line)
                self.process.stdin.flush()
            except Exception as e:
                print(f"Error during KaTeX setup: {e}")
            
        except Exception as e:
            print(f"Error starting KaTeX renderer: {e}")
            self.process = None
        
        return config

    def _render_latex(self, latex, display_mode=False):
        # Check cache first
        latex_trimmed = latex.strip()
        cache_key = None
        if self.db_conn:
            try:
                # Create a unique hash for the content AND display mode
                content_to_hash = f"{latex_trimmed}::{display_mode}"
                cache_key = hashlib.sha256(content_to_hash.encode('utf-8')).hexdigest()
                
                cursor = self.db_conn.execute("SELECT html FROM katex_cache WHERE hash=?", (cache_key,))
                row = cursor.fetchone()
                if row:
                    return row[0]
            except Exception as e:
                print(f"Error reading cache: {e}")

        if not self.process:
            return None
        
        with self.lock:
            payload = {
                'type': 'render',
                'latex': latex,
                'displayMode': display_mode,
                'options': self.config['katex_options']
            }
            try:
                line = (json.dumps(payload) + '\n').encode('utf-8')
                self.process.stdin.write(line)
                self.process.stdin.flush()
                
                response_line = self.process.stdout.readline()
                if not response_line:
                    return None
                
                result = json.loads(response_line.decode('utf-8'))
                if result.get('status') == 'success':
                    html = result.get('html')
                    # Save to cache
                    if self.db_conn and cache_key:
                        try:
                            with self.db_conn:
                                self.db_conn.execute(
                                    "INSERT OR REPLACE INTO katex_cache (hash, html) VALUES (?, ?)",
                                    (cache_key, html)
                                )
                        except Exception as e:
                            print(f"Error saving to cache: {e}")
                    return html
                else:
                    print(f"KaTeX error: {result.get('message')}")
            except Exception as e:
                if self.process and self.process.poll() is not None:
                    stderr_content = self.process.stderr.read()
                    if stderr_content:
                        print(f"Renderer died with: {stderr_content.decode('utf-8', errors='replace')}")
            return None

    def on_post_page(self, output, page, config):
        if not self.process:
            return output

        soup = BeautifulSoup(output, 'html.parser')
        math_elements = soup.find_all(class_='arithmatex')
        for el in math_elements:
            content = el.get_text(strip=True)
            display_mode = False
            
            if content.startswith('\\(') and content.endswith('\\)'):
                latex = content[2:-2]
            elif content.startswith('\\[') and content.endswith('\\]'):
                latex = content[2:-2]
                display_mode = True
            elif content.startswith('$') and content.endswith('$'):
                latex = content[1:-1]
            elif content.startswith('$$') and content.endswith('$$'):
                latex = content[2:-2]
                display_mode = True
            else:
                latex = content
            
            rendered_html = self._render_latex(latex, display_mode)
            new_soup = BeautifulSoup(rendered_html, 'html.parser')

            el.clear()
            el.append(new_soup)

        # Assets Injection
        css_file = self.config['katex_css_filename']
        if self.config['add_katex_css']:
            if self.config['embed_assets'] and self._local_dist_path:
                dest_path = self.config['copy_assets_to']
                css_dest_file = f"{dest_path}/{css_file}"
                css_url = get_relative_url(css_dest_file, page.url)
                css_link = soup.new_tag('link', rel='stylesheet', href=css_url)
                if soup.head:
                    soup.head.append(css_link)
                else:
                    soup.insert(0, css_link)
            else:
                css_url = self._resolve_url(self.config['katex_dist'], css_file)
                css_link = soup.new_tag('link', rel='stylesheet', href=css_url)
                if soup.head:
                    soup.head.append(css_link)
                else:
                    soup.insert(0, css_link)

        # Inject ONLY client_scripts
        for script_name in self.config['client_scripts']:
            if '://' in script_name or script_name.endswith('.js'):
                script_url = script_name
            else:
                if self.config['embed_assets'] and self._local_dist_path:
                     dest_path = self.config['copy_assets_to']
                     script_dest_file = f"{dest_path}/contrib/{script_name}.min.js"
                     script_url = get_relative_url(script_dest_file, page.url)
                else:
                    script_url = self._resolve_url(self.config['katex_dist'], f'contrib/{script_name}.min.js')
            
            script_tag = soup.new_tag('script', src=script_url)
            if soup.body:
                soup.body.append(script_tag)
            else:
                soup.append(script_tag)

        return str(soup)

    def on_post_build(self, config):
        if self.process:
            self.process.terminate()
            self.process.wait()
        
        if self.db_conn:
            try:
                self.db_conn.close()
            except:
                pass
            self.db_conn = None
        
        # Copy assets if requested
        if self.config['embed_assets'] and self._local_dist_path:
            dest_dir = os.path.join(config['site_dir'], self.config['copy_assets_to'])
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            
            # Copy katex CSS (filename depends on config)
            css_file = self.config['katex_css_filename']
            src_css = os.path.join(self._local_dist_path, css_file)
            if os.path.exists(src_css):
                shutil.copy2(src_css, dest_dir)
            else:
                print(f"Warning: Could not find {css_file} at {src_css}")
            
            # Copy fonts
            src_fonts = os.path.join(self._local_dist_path, 'fonts')
            dest_fonts = os.path.join(dest_dir, 'fonts')
            if os.path.exists(src_fonts):
                if os.path.exists(dest_fonts):
                    shutil.rmtree(dest_fonts)
                shutil.copytree(src_fonts, dest_fonts)
            
            # Copy requested client_scripts
            dest_contrib = os.path.join(dest_dir, 'contrib')
            if not os.path.exists(dest_contrib):
                 os.makedirs(dest_contrib, exist_ok=True)
            
            # Note: We technically might need to copy items from ssr_contribs IF the user wanted them
            # but we decided they are separate. However, if 'mhchem' is in ssr_contribs only,
            # we don't copy it. If user wants it on client, they MUST put it in client_scripts.
            for script_name in self.config['client_scripts']:
                 if '://' not in script_name and not script_name.endswith('.js'):
                     src_script = os.path.join(self._local_dist_path, 'contrib', f'{script_name}.min.js')
                     if os.path.exists(src_script):
                         shutil.copy2(src_script, dest_contrib)


