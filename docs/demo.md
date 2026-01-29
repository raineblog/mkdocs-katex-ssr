# Math Rendering Demo

This page demonstrates the server-side rendering capabilities of the plugin.

## Inline Math

The equation $E = mc^2$ is rendered inline.

## Block Math

$$
\frac{1}{2\pi}\int_{-\infty}^{\infty} e^{-\frac{x^2}{2}} dx = 1
$$

## Matrices

$$
\begin{pmatrix}
a & b \\
c & d
\end{pmatrix}
$$

## Chemical Formulas (mhchem)

Since `mhchem` is enabled in `ssr_contribs`:

$$
\ce{CO2 + C -> 2 CO}
$$

$$
\ce{Hg^2+ ->[I-] HgI2 ->[I-] [Hg^{II}I4]^2-}
$$

## Macros

Using the defined macro `\RR`:

$$
x \in \RR
$$
