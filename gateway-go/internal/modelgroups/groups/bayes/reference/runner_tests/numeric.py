"""How the runner's tests compare two numbers that the same arithmetic produces in a different ORDER.

Two computations of one statistic over blocks of different sizes, or over an array of another shape, add the same terms in another
order, and a floating-point sum depends on its order: numpy picks its kernels from the CPU (SSE, AVX2, AVX-512, NEON) and from the
shape and the memory layout of the array, so the last bits of a mean, a variance or a quantile's interpolation differ from one
machine to another although each machine is deterministic. A test of that relation (the budgets, the blocks, the whole array against
its pieces) must therefore not ask for the same bits. It asks for the same number up to what reordering the sum can change.

That is REORDER_RTOL. Adding n terms in any order changes the result by at most n * eps * sum(|x_i|) (eps = 2.2e-16, the worst case
of the plain left-to-right sum; numpy's pairwise sum is far better, about log2(n) * eps). The draws of these tests are a few
thousand values (at most 10^4: 2.2e-12 as a bound on the sum), so a relative 1e-11 holds five times the worst case, and nothing is
hidden under it: a different formula, a block lost or a column shifted moves a number by far more (a single changed draw of 6,000
moves a mean by 1e-4 of its spread).

What is NOT compared with it: what must be the same bytes. Counts, integers, strings, the order of rows, the seed's streams (a
generator gives the same stream on any CPU) and the files a run writes stay `assertEqual`.
"""
import math

REORDER_RTOL = 1e-11


def close(want, got, rtol=REORDER_RTOL, atol=0.0):
    """True when two floats are the same up to a reordered sum: both missing, both NaN, or within rtol of the larger one (plus atol)."""
    if want is None or got is None:
        return want is None and got is None
    if math.isnan(want) or math.isnan(got):
        return math.isnan(want) and math.isnan(got)
    if math.isinf(want) or math.isinf(got):
        return want == got
    return abs(want - got) <= rtol * max(abs(want), abs(got)) + atol


def assert_same_numbers(test, want, got, rtol=REORDER_RTOL, atol=0.0, what="value"):
    """Walk two nested results (dicts, lists, tuples, columns read from a parquet file): every float within rtol, everything else
    (integers, booleans, strings, None, the structure itself) equal."""
    if isinstance(want, dict):
        test.assertEqual(list(want), list(got), f"{what}: the keys")
        for k in want:
            assert_same_numbers(test, want[k], got[k], rtol, atol, f"{what}[{k!r}]")
    elif isinstance(want, (list, tuple)):
        test.assertEqual(len(want), len(got), f"{what}: the length")
        for i, (a, b) in enumerate(zip(want, got)):
            assert_same_numbers(test, a, b, rtol, atol, f"{what}[{i}]")
    elif isinstance(want, float):
        test.assertTrue(close(float(want), None if got is None else float(got), rtol, atol), f"{what}: {want!r} != {got!r}")
    else:
        test.assertEqual(want, got, what)
