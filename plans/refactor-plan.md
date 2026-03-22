# MkDocs KaTeX SSR 插件重构计划

## 项目概述

**项目名称**: mkdocs-katex-ssr  
**当前版本**: 1.1.2  
**目标版本**: 1.2.0  
**重构目标**: 删除 sqlite3 兼容代码，完全采用 lmdb 缓存，优化整体逻辑

---

## 一、当前代码问题分析

### 1.1 需要删除的 sqlite3 兼容代码

#### 位置 1: `plugin.py` 第 76-96 行
```python
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
            for key, value in rows:
                lmdb_cache.set(key, value)
        
        conn.close()
        os.remove(sqlite_path)
        log.info("Katex-SSR: Migration completed and old cache removed.")
    except Exception as e:
        log.error(f"Katex-SSR: Migration failed: {e}")
```

#### 位置 2: `plugin.py` 第 153-155 行
```python
# Migration check
sqlite_path = os.path.join(cache_dir, 'cache.db')
LmdbCache.migrate_from_sqlite(sqlite_path, self.cache)
```

### 1.2 需要保留的生产环境修复（重要！）

以下代码看起来"有点违和"，但实际上是长期生产中修复的 bug，**必须保留**：

#### 修复 1: 激进的警告过滤器（第 17-33 行）
```python
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
```
**原因**: 这是为了解决 pkg_resources、jieba 等库的警告污染构建日志的问题。

#### 修复 2: LMDB 配置（第 48-52 行）
```python
self.env = lmdb.open(
    self.cache_dir,
    map_size=self.map_size,
    writemap=True,
    map_async=True,
    metasync=False,
    sync=False,
    max_dbs=1
)
```
**原因**: 这些配置是为了性能优化，writemap=True 和 sync=False 可以显著提高写入性能。

#### 修复 3: MapFullError 动态扩展（第 64-70 行）
```python
except lmdb.MapFullError:
    # Dynamically double the map_size if full
    self.map_size *= 2
    log.info(f"Katex-SSR: Cache full, increasing map_size to {self.map_size / 1024 / 1024:.0f}MB")
    self.env.close()
    self._open_env()
    self.set(key, value)
```
**原因**: 这是防止缓存空间不足导致构建失败的关键修复。

#### 修复 4: Windows shell 处理（第 222-225 行）
```python
use_shell = os.name == 'nt'
cmd = [self.runtime, renderer_path]
if use_shell:
     cmd = f'{self.runtime} "{renderer_path}"'
```
**原因**: Windows 下 subprocess 需要 shell=True 才能正确执行 node/bun 命令。

#### 修复 5: stderr 日志线程（第 246-257 行）
```python
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
```
**原因**: 这是为了避免管道阻塞导致的死锁问题。

#### 修复 6: CHUNK_SIZE 分块处理（第 284-286 行）
```python
# 将公式分块处理，防止管道溢出和内存暴涨导致的死锁 (Pipe Deadlock)
# 建议块大小在 200-500 之间，这样 JSON 负载通常不会超过几百 KB
CHUNK_SIZE = 500
```
**原因**: 这是防止管道溢出和内存暴涨导致的死锁的关键修复。

#### 修复 7: 控制字符清理（第 289-291 行）
```python
# 移除 \x00-\x1f 控制字符，保留 \t, \n, \r
# 这是为了防止用户 Markdown 包含破坏 json 解析的脏数据
control_chars_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
```
**原因**: 用户 Markdown 可能包含控制字符，会破坏 JSON 解析。

#### 修复 8: JSON strict=False（第 317 行）
```python
result = json.loads(response_line.decode('utf-8', errors='replace'), strict=False)
```
**原因**: 允许 JSON 中包含控制字符，防止解析失败。

#### 修复 9: 进程死亡检测（第 327-331 行）
```python
if self.process and self.process.poll() is not None:
    stderr_content = self.process.stderr.read()
    if stderr_content:
        log.error(f"Renderer died with: {stderr_content.decode('utf-8', errors='replace')}")
```
**原因**: 检测渲染进程是否意外关闭，并输出错误信息。

### 1.3 代码冗余问题

#### 问题 1: URL 处理方法冗余
- `_ensure_trailing_slash()` 和 `_resolve_url()` 方法可以简化
- 建议：合并为更简洁的实现

#### 问题 2: 资产注入逻辑重复
- `on_post_page()` 方法中的 CSS 和 JS 注入逻辑有大量重复
- 建议：提取为独立方法

#### 问题 3: 错误处理不一致
- 有些地方用 `print()`，有些用 `log.error()`
- 建议：统一使用 logging 模块

#### 问题 4: 配置验证逻辑分散
- `contrib_scripts` 的兼容性处理可以简化
- 建议：集中配置验证逻辑

### 1.4 文档问题

#### 问题 1: README.md
- 第 17 行提到 "4GB limit by default"，但实际代码中是 32MB 起始
- 需要更新缓存说明

#### 问题 2: configuration.md
- 第 15 行提到 "4GB virtual memory address space"，需要更准确的描述

#### 问题 3: getting-started.md
- 第 21 行有语法错误（重复的 ```bash）

---

## 二、重构详细计划

### 2.1 删除 sqlite3 兼容代码

#### 步骤 1: 删除 `migrate_from_sqlite()` 方法
- 删除 `LmdbCache` 类中的 `migrate_from_sqlite()` 静态方法
- 删除相关的 `import sqlite3` 语句（如果存在）

#### 步骤 2: 删除迁移检查代码
- 删除 `on_config()` 方法中的迁移检查代码
- 简化缓存初始化逻辑

### 2.2 重构 `LmdbCache` 类

#### 优化 1: 简化初始化
```python
class LmdbCache:
    def __init__(self, cache_dir, initial_map_size=32 * 1024 * 1024):
        self.cache_dir = cache_dir
        self.map_size = initial_map_size
        self.env = self._open_env()
```

#### 优化 2: 改进错误处理
- 添加更详细的错误日志
- 统一使用 logging 模块

#### 优化 3: 添加上下文管理器支持
```python
def __enter__(self):
    return self

def __exit__(self, exc_type, exc_val, exc_tb):
    self.close()
```

### 2.3 优化 URL 处理方法

#### 优化前:
```python
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
```

#### 优化后:
```python
def _ensure_trailing_slash(self, path):
    """确保路径以斜杠结尾"""
    return path if path.endswith(('/', '\\')) else f"{path}/"

def _resolve_url(self, base, path):
    """解析 URL 或文件路径"""
    base = base.replace('\\', '/')
    if base.startswith('http'):
        return f"{base.rstrip('/')}/{path.lstrip('/')}"
    return os.path.normpath(os.path.join(base, path))
```

### 2.4 提取资产注入逻辑

#### 新增方法:
```python
def _inject_css(self, soup, page):
    """注入 CSS 链接"""
    css_file = self.config['katex_css_filename']
    if not self.config['add_katex_css']:
        return
    
    if self.config['embed_assets'] and self._local_dist_path:
        dest_path = self.config['copy_assets_to']
        css_dest_file = f"{dest_path}/{css_file}"
        css_url = get_relative_url(css_dest_file, page.url)
    else:
        css_url = self._resolve_url(self.config['katex_dist'], css_file)
    
    css_link = soup.new_tag('link', rel='stylesheet', href=css_url)
    if soup.head:
        soup.head.append(css_link)
    else:
        soup.insert(0, css_link)

def _inject_scripts(self, soup, page, scripts, script_type='client'):
    """注入 JavaScript 脚本"""
    for script_name in scripts:
        if '://' in script_name or script_name.endswith('.js'):
            script_url = script_name
        elif self.config['embed_assets'] and self._local_dist_path:
            dest_path = self.config['copy_assets_to']
            script_dest_file = f"{dest_path}/contrib/{script_name}.min.js"
            script_url = get_relative_url(script_dest_file, page.url)
        else:
            script_url = self._resolve_url(
                self.config['katex_dist'], 
                f'contrib/{script_name}.min.js'
            )
        
        script_tag = soup.new_tag('script', src=script_url)
        if soup.body:
            soup.body.append(script_tag)
        else:
            soup.append(script_tag)
```

### 2.5 统一错误处理

#### 原则:
1. 所有错误日志使用 `log.error()` 或 `log.warning()`
2. 移除所有 `print()` 语句
3. 异常处理时记录详细上下文

#### 示例:
```python
# 优化前
print(f"Error starting KaTeX renderer: {e}")

# 优化后
log.error(f"Failed to start KaTeX renderer: {e}", exc_info=True)
```

### 2.6 简化配置验证

#### 优化前:
```python
# Merge legacy contrib_scripts into ssr_contribs if used
if self.config['contrib_scripts']:
    for script in self.config['contrib_scripts']:
        if script not in self.config['ssr_contribs']:
            self.config['ssr_contribs'].append(script)
```

#### 优化后:
```python
# 合并遗留的 contrib_scripts 到 ssr_contribs
if self.config['contrib_scripts']:
    self.config['ssr_contribs'] = list(set(
        self.config['ssr_contribs'] + self.config['contrib_scripts']
    ))
```

---

## 三、文档更新计划

### 3.1 README.md 更新

#### 更新内容:
1. 更新缓存说明（从 "4GB limit" 改为更准确的描述）
2. 移除任何 sqlite3 相关说明
3. 更新版本号引用

#### 关键修改:
```markdown
- **🗃️ Built-in LMDB Cache**: Recompilation is instantaneous thanks to the integrated ultra-fast LMDB engine (dynamic map size, starts at 32MB).
```

### 3.2 configuration.md 更新

#### 更新内容:
1. 更新 LMDB 缓存说明
2. 移除 sqlite3 相关内容
3. 优化配置选项描述

#### 关键修改:
```markdown
> [!TIP]
> **LMDB Cache**: The plugin uses LMDB for high-performance caching. The cache starts at 32MB and dynamically expands as needed.
```

### 3.3 getting-started.md 更新

#### 更新内容:
1. 修复第 21 行的语法错误
2. 更新安装说明

#### 关键修改:
```markdown
```bash
git clone https://github.com/raineblog/mkdocs-katex-ssr.git
cd mkdocs-katex-ssr
pip install .
```
```

### 3.4 index.md 更新

#### 更新内容:
1. 更新特性说明
2. 移除过时的描述

### 3.5 troubleshooting.md 更新

#### 更新内容:
1. 添加 LMDB 缓存相关故障排除
2. 优化现有故障排除说明

---

## 四、版本更新计划

### 4.1 pyproject.toml 更新

```toml
[project]
name = "mkdocs-katex-ssr"
version = "1.2.0"  # 从 1.1.2 更新到 1.2.0
description = "A MkDocs plugin for server-side rendering of KaTeX math."
```

---

## 五、测试计划

### 5.1 本地构建测试

```bash
# 清理旧的构建产物
rm -rf dist/ build/ *.egg-info

# 使用 uv 构建
uv build

# 验证构建产物
ls -la dist/
```

### 5.2 本地运行测试

```bash
# 安装插件
uv pip install -e .

# 运行 MkDocs 构建
uv run mkdocs build

# 检查生成的站点
ls -la site/
```

### 5.3 功能验证

1. **缓存功能**: 验证 LMDB 缓存正常工作
2. **SSR 渲染**: 验证数学公式正确渲染
3. **离线模式**: 验证 `embed_assets` 功能
4. **客户端渲染**: 验证 `disable: true` 模式

---

## 六、Git 提交计划

### 6.1 提交信息

```
refactor: Remove sqlite3 compatibility, optimize LMDB cache

- Remove sqlite3 v1.0.x compatibility and migration code
- Simplify LmdbCache class with better error handling
- Optimize warning filtering (less aggressive)
- Extract asset injection logic into reusable methods
- Unify error handling (use logging instead of print)
- Update documentation to reflect LMDB-only caching
- Bump version to 1.2.0
```

### 6.2 Git Tag

```bash
git tag -a v1.2.0 -m "Version 1.2.0: LMDB-only caching, code optimization"
git push origin v1.2.0
```

---

## 七、执行顺序

1. **第一阶段**: 删除 sqlite3 兼容代码
   - 删除 `migrate_from_sqlite()` 方法
   - 删除迁移检查代码

2. **第二阶段**: 重构核心代码
   - 优化 `LmdbCache` 类
   - 优化 URL 处理方法
   - 提取资产注入逻辑
   - 统一错误处理

3. **第三阶段**: 更新文档
   - 更新 README.md
   - 更新 configuration.md
   - 更新 getting-started.md
   - 更新 index.md
   - 更新 troubleshooting.md

4. **第四阶段**: 版本更新和测试
   - 更新 pyproject.toml 版本号
   - 本地构建测试
   - 本地运行测试

5. **第五阶段**: 提交和推送
   - Git commit
   - Git tag v1.2.0
   - Git push

---

## 八、风险评估

### 8.1 潜在风险

1. **删除 sqlite3 兼容代码**: 
   - 风险：现有用户如果从旧版本升级，可能丢失缓存
   - 缓解：在 CHANGELOG 中明确说明

2. **资产注入逻辑重构**:
   - 风险：可能影响 CSS/JS 注入
   - 缓解：充分测试各种配置

### 8.2 回滚计划

如果重构后出现问题：
1. 使用 `git revert` 回滚提交
2. 保留 v1.1.2 tag 作为备份
3. 在 PyPI 上保留旧版本

---

## 九、成功标准

1. ✅ 所有 sqlite3 兼容代码已删除
2. ✅ 代码行数减少 15% 以上
3. ✅ 所有测试通过
4. ✅ 文档已更新
5. ✅ 版本号已更新为 1.2.0
6. ✅ Git tag v1.2.0 已创建并推送

---

**计划创建时间**: 2026-03-22  
**计划执行者**: Kilo Code  
**审核状态**: 待用户审核
