import os
import json
import hashlib
import subprocess
import threading
import warnings
import logging
import shutil
import time
import lmdb
from mkdocs.plugins import BasePlugin
from mkdocs.config import config_options
from mkdocs.utils import get_relative_url
from bs4 import BeautifulSoup
import re

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

log = logging.getLogger('mkdocs.plugins.katex-ssr')

class LmdbCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.map_size = 32 * 1024 * 1024  # Start with 1GB (conservative for virtual memory)
        self._open_env()

    def _open_env(self):
        # metasync=False, sync=False for maximal performance as requested
        self.env = lmdb.open(
            self.cache_dir,
            map_size=self.map_size,
            writemap=True,
            map_async=True,
            metasync=False,
            sync=False,
            max_dbs=1
        )

    def get(self, key):
        with self.env.begin() as txn:
            value = txn.get(key.encode('utf-8'))
            return value.decode('utf-8') if value else None

    def set(self, key, value):
        try:
            with self.env.begin(write=True) as txn:
                txn.put(key.encode('utf-8'), value.encode('utf-8'))
        except lmdb.MapFullError:
            # Dynamically double the map_size if full
            self.map_size *= 2
            log.info(f"Katex-SSR: Cache full, increasing map_size to {self.map_size / 1024 / 1024:.0f}MB")
            self.env.close()
            self._open_env()
            self.set(key, value)

    def close(self):
        if self.env:
            self.env.close()

    @staticmethod
    def migrate_from_sqlite(sqlite_path, lmdb_cache):
        if not os.path.exists(sqlite_path):
            return
        
        try:
            import sqlite3
            conn = sqlite3.connect(sqlite_path)
            cursor = conn.execute("SELECT hash, html FROM katex_cache")
            rows = cursor.fetchall()
            
            if rows:
                log.info(f"Katex-SSR: Migrating {len(rows)} items from SQLite3 to LMDB...")
                for i, (key, value) in enumerate(rows):
                    lmdb_cache.set(key, value)
                    if (i + 1) % 1000 == 0:
                        log.info(f"Katex-SSR: Migrated {i + 1}/{len(rows)} items...")
            
            conn.close()
            os.remove(sqlite_path)
            log.info("Katex-SSR: Migration completed and old cache removed.")
        except Exception as e:
            log.error(f"Katex-SSR: Migration failed: {e}")

class KatexSsrPlugin(BasePlugin):
    config_scheme = (
        ('verbose', config_options.Type(bool, default=False)),
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
        ('disable', config_options.Type(bool, default=False)),
        ('use_bun', config_options.Choice(('auto', True, False), default='auto')),
    )

    def __init__(self):
        self.process = None
        self.lock = threading.Lock()
        self._asset_cache = {}
        self._local_dist_path = None
        self.cache = None
        self.total_formulas = 0
        self.total_cached = 0
        self.total_time = 0

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
        if self.config['disable'] and not self.config['add_katex_css']:
            raise config_options.ValidationError(
                "When 'disable' is true, 'add_katex_css' must also be true to ensure KaTeX resources are available for client-side rendering."
            )

        self.config['katex_dist'] = self._ensure_trailing_slash(self.config['katex_dist'])
        
        project_dir = os.path.dirname(config['config_file_path'])
        
        # Initialize Cache
        try:
            cache_dir = os.path.join(project_dir, '.cache', 'plugin', 'katex-ssr')
            os.makedirs(cache_dir, exist_ok=True)
            self.cache = LmdbCache(cache_dir)
            
            # Migration check
            sqlite_path = os.path.join(cache_dir, 'cache.db')
            LmdbCache.migrate_from_sqlite(sqlite_path, self.cache)
        except Exception as e:
            log.error(f"Warning: Failed to initialize KaTeX SSR cache: {e}")
            self.cache = None
        
        # Merge legacy contrib_scripts into ssr_contribs if used
        if self.config['contrib_scripts']:
            # Append unique items
            for script in self.config['contrib_scripts']:
                if script not in self.config['ssr_contribs']:
                    self.config['ssr_contribs'].append(script)

        # Detect runtime environment (Node vs Bun)
        self.runtime = 'node'
        self.pm = 'npm'
        use_bun_cfg = self.config['use_bun']
        
        has_bun = shutil.which('bun') is not None
        has_node = shutil.which('node') is not None
        
        if use_bun_cfg is True:
            if not has_bun:
                raise config_options.ValidationError("配置指定了 use_bun=True，但在系统中未找到 bun。")
            self.runtime = 'bun'
            self.pm = 'bun'
        elif use_bun_cfg is False:
            if not has_node:
                raise config_options.ValidationError("配置指定了 use_bun=False，但在系统中未找到 node。")
            self.runtime = 'node'
            self.pm = 'npm'
        else:
            if has_bun:
                self.runtime = 'bun'
                self.pm = 'bun'
            elif has_node:
                self.runtime = 'node'
                self.pm = 'npm'
            elif not self.config['disable']:
                raise config_options.ValidationError("系统中未找到 node 或 bun，无法启动 KaTeX SSR。请安装 node 或 bun，或者将 disable 设为 true。")

        # Asset resolution logic
        possible_dist = self._resolve_url(project_dir, self.config['katex_dist'])
        if os.path.isdir(possible_dist):
             self._local_dist_path = possible_dist
        else:
             node_modules = os.path.join(project_dir, 'node_modules')
             dist = os.path.join(node_modules, 'katex', 'dist')
             if os.path.isdir(dist):
                 self._local_dist_path = dist

        katex_dir = os.path.join(project_dir, 'node_modules', 'katex')
        if not self.config['disable'] and not os.path.isdir(katex_dir):
            log.info(f"Katex-SSR: 未检测到 katex 依赖，正在使用 {self.pm} 自动安装...")
            install_cmd = [self.pm, 'add', 'katex'] if self.pm == 'bun' else [self.pm, 'install', 'katex']
            try:
                subprocess.run(install_cmd, cwd=project_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.info("Katex-SSR: katex 安装成功。")
                self._local_dist_path = os.path.join(katex_dir, 'dist')
            except Exception as e:
                log.error(f"Katex-SSR: katex 安装失败: {e}")

        if self.config['disable']:
            return config

        # Start renderer process
        renderer_path = os.path.join(os.path.dirname(__file__), 'renderer.js')
        
        use_shell = os.name == 'nt'
        cmd = [self.runtime, renderer_path]
        if use_shell:
             cmd = f'{self.runtime} "{renderer_path}"'
        
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
            
            # Start stderr logging thread to avoid mixed output and pipe blockage
            def log_stderr(pipe):
                for line in iter(pipe.readline, b''):
                    msg = line.decode('utf-8', errors='replace').strip()
                    if msg:
                        if 'error' in msg.lower():
                            log.error(f"Katex-SSR Node Error: {msg}")
                        else:
                            log.debug(f"Katex-SSR Node: {msg}")
                pipe.close()
            
            error_thread = threading.Thread(target=log_stderr, args=(self.process.stderr,), daemon=True)
            error_thread.start()
            
            log.info(f"Katex-SSR: 成功使用 {self.runtime} 启动后端渲染进程。")
            
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

    def _render_latex_batch(self, items):
        if not self.process or not items:
            return {}
        
        # 将公式分块处理，防止管道溢出和内存暴涨导致的死锁 (Pipe Deadlock)
        # 建议块大小在 200-500 之间，这样 JSON 负载通常不会超过几百 KB
        CHUNK_SIZE = 500
        all_results = {}
        
        # 移除 \x00-\x1f 控制字符，保留 \t, \n, \r
        # 这是为了防止用户 Markdown 包含破坏 json 解析的脏数据
        control_chars_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
        
        with self.lock:
            for i in range(0, len(items), CHUNK_SIZE):
                chunk = items[i:i + CHUNK_SIZE]
                
                # 清洗 chunk 内部的 latex
                for item in chunk:
                    if 'latex' in item:
                        item['latex'] = control_chars_re.sub('', item['latex'])
                
                payload = {
                    'type': 'render_batch',
                    'items': chunk,
                    'options': self.config['katex_options']
                }
                try:
                    line = (json.dumps(payload) + '\n').encode('utf-8')
                    self.process.stdin.write(line)
                    self.process.stdin.flush()
                    
                    response_line = self.process.stdout.readline()
                    if not response_line:
                        log.error("Katex-SSR: 渲染进程意外关闭。")
                        break
                    
                    result = json.loads(response_line.decode('utf-8', errors='replace'), strict=False)
                    if result.get('status') == 'success':
                        for res in result.get('results', []):
                            if res.get('status') == 'success':
                                all_results[res['id']] = res.get('html')
                            else:
                                log.warning(f"KaTeX error for item {res['id']}: {res.get('message')}")
                    else:
                        log.warning(f"KaTeX batch error: {result.get('message')}")
                except Exception as e:
                    if self.process and self.process.poll() is not None:
                        stderr_content = self.process.stderr.read()
                        if stderr_content:
                            log.error(f"Renderer died with: {stderr_content.decode('utf-8', errors='replace')}")
                    log.error(f"Katex-SSR IPC Error: {e}")
                    break
        return all_results


    def on_post_page(self, output, page, config):
        if self.config['disable']:
            soup = BeautifulSoup(output, 'html.parser')
        else:
            if not self.process:
                return output

            start_time = time.time()
            formula_count = 0
            cache_count = 0

            soup = BeautifulSoup(output, 'html.parser')
            math_elements = soup.find_all(class_='arithmatex')
            
            batch_items = []
            cached_results = {}
            
            for i, el in enumerate(math_elements):
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
                
                latex_trimmed = latex.strip()
                cache_key = None
                from_cache = False
                
                if self.cache:
                    try:
                        content_to_hash = f"{latex_trimmed}::{display_mode}"
                        cache_key = hashlib.sha256(content_to_hash.encode('utf-8')).hexdigest()
                        row = self.cache.get(cache_key)
                        if row:
                            cached_results[i] = row
                            from_cache = True
                            cache_count += 1
                    except Exception as e:
                        pass
                
                if not from_cache:
                    batch_items.append({
                        'id': i,
                        'latex': latex,
                        'displayMode': display_mode,
                        'cache_key': cache_key
                    })

            batch_results = self._render_latex_batch(batch_items)
            
            if self.cache and batch_results:
                try:
                    for item in batch_items:
                        i = item['id']
                        if i in batch_results and item['cache_key']:
                            self.cache.set(item['cache_key'], batch_results[i])
                except Exception as e:
                    log.warning(f"Error saving to cache: {e}")
            
            for i, el in enumerate(math_elements):
                html = None
                if i in cached_results:
                    html = cached_results[i]
                elif i in batch_results:
                    html = batch_results[i]
                
                if html:
                    new_soup = BeautifulSoup(html, 'html.parser')
                    el.clear()
                    el.append(new_soup)

            page_duration = time.time() - start_time
            self.total_formulas += len(math_elements)
            self.total_cached += cache_count
            self.total_time += page_duration

            if self.config['verbose']:
                duration_ms = page_duration * 1000
                log.info(f"Katex-SSR processed {page.file.src_path} in {duration_ms:.2f}ms: {len(math_elements)} formulas ({cache_count} cached)")

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

        # Inject Auto-render if disabled
        if self.config['disable']:
            # 1. Inject KaTeX JS
            if self.config['embed_assets'] and self._local_dist_path:
                dest_path = self.config['copy_assets_to']
                js_url = get_relative_url(f"{dest_path}/katex.min.js", page.url)
            else:
                js_url = self._resolve_url(self.config['katex_dist'], "katex.min.js")
            
            katex_js = soup.new_tag('script', src=js_url)
            if soup.body:
                soup.body.append(katex_js)
            else:
                soup.append(katex_js)

            # 2. Inject Auto-render JS
            if self.config['embed_assets'] and self._local_dist_path:
                dest_path = self.config['copy_assets_to']
                auto_url = get_relative_url(f"{dest_path}/contrib/auto-render.min.js", page.url)
            else:
                auto_url = self._resolve_url(self.config['katex_dist'], "contrib/auto-render.min.js")
            
            auto_js = soup.new_tag('script', src=auto_url)
            if soup.body:
                soup.body.append(auto_js)
            else:
                soup.append(auto_js)

            # 3. Inject ssr_contribs (they are needed on client if SSR is disabled)
            for script_name in self.config['ssr_contribs']:
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

            # 4. Inject auto-render init code
            macros = self.config['katex_options'].get('macros', {})
            # Standard delimiters for arithmatex generic mode
            auto_render_script = f"""
            document.addEventListener("DOMContentLoaded", function() {{
                renderMathInElement(document.body, {{
                    delimiters: [
                        {{left: "$$", right: "$$", display: true}},
                        {{left: "$", right: "$", display: false}},
                        {{left: "\\\\(", right: "\\\\)", display: false}},
                        {{left: "\\\\[", right: "\\\\]", display: true}}
                    ],
                    macros: {json.dumps(macros)},
                    ...{json.dumps(self.config['katex_options'])}
                }});
            }});
            """
            init_tag = soup.new_tag('script')
            init_tag.string = auto_render_script
            if soup.body:
                soup.body.append(init_tag)
            else:
                soup.append(init_tag)

        return str(soup)

    def on_post_build(self, config):
        if not self.config['disable']:
            log.info(f"Katex-SSR: 构建完毕。共处理 {self.total_formulas} 个数学公式 ({self.total_cached} 个来自缓存)，总耗时 {self.total_time:.2f} 秒。")

        if self.process:
            try:
                # 显式关闭标准输入，发送 EOF 信号给 Node/Bun 进程
                if self.process.stdin:
                    self.process.stdin.close()
            except:
                pass
            
            try:
                # 给予 5 秒缓冲时间让其自行收尾退出
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Katex-SSR: 渲染进程退出超时，正在强制结束...")
                # 如果是 Windows 且使用了 Shell，这里可能会留下残留进程
                # 但由于我们现在是直接运行或者已经通过 stdin.close() 处理，通常能解决
                self.process.kill()
                self.process.wait()
        
        if self.cache:
            try:
                self.cache.close()
            except:
                pass
            self.cache = None
        
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

            if self.config['disable']:
                # Copy katex.min.js
                src_js = os.path.join(self._local_dist_path, "katex.min.js")
                if os.path.exists(src_js):
                    shutil.copy2(src_js, dest_dir)
                
                # Copy auto-render.min.js
                src_auto = os.path.join(self._local_dist_path, "contrib", "auto-render.min.js")
                if os.path.exists(src_auto):
                    shutil.copy2(src_auto, dest_contrib)
                
                # Copy ssr_contribs as well
                for script_name in self.config['ssr_contribs']:
                    if '://' not in script_name and not script_name.endswith('.js'):
                        src_script = os.path.join(self._local_dist_path, 'contrib', f'{script_name}.min.js')
                        if os.path.exists(src_script):
                            shutil.copy2(src_script, dest_contrib)
