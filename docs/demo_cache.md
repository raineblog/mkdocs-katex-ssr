# KaTeX 缓存与解析测试页

## 1. 分隔符匹配测试

Inline 公式: $E=mc^2$

Block 公式 (应使用 katex-display):

$$
\int_a^b f(x) dx = F(b) - F(a)
$$

另一个 Inline: \(x+y\)

另一个 Block:

\[
\sum_{i=1}^n i = \frac{n(n+1)}{2}
\]

## 2. 全局宏警告测试

下面这个公式包含 `\gdef`，应该在构建日志中输出警告：

$$ \gdef\foo#1{f(#1)} $$

使用宏: $$ \foo{x} $$

## 3. 缓存一致性测试

定义一个在 `mkdocs.yml` 中已经存在的宏 `\RR`:

$$ \RR^n $$

如果修改 `mkdocs.yml` 中的 `\RR` 定义，版本校验机制应该确保此公式重新渲染。
