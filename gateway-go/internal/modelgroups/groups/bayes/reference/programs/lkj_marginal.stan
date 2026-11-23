// The self-test's check 8 (docs/specs/bayes-image.md, «The self-test»): for one correlation r of a K x K correlation matrix
// with an LKJ(eta) distribution, (r + 1) / 2 ~ Beta(b, b) with b = eta - 1 + K / 2 (Lewandowski, Kurowicka and Joe 2009).
// Run with the fixed_param sampler: each iteration draws, for every case, a Cholesky factor with Stan's own generator, forms
// Omega = L L' and writes the probability integral transform u = beta_cdf((r + 1) / 2 | b, b) of Omega[2, 1] and of Omega[K, 1]
// (the same element when K is 2), which is uniform when the binding is right.
data {
  int<lower=1> C;
  array[C] int<lower=2> K;
  array[C] real<lower=0> eta;
}
generated quantities {
  array[C] real u_21;
  array[C] real u_k1;
  for (c in 1:C) {
    matrix[K[c], K[c]] Omega = multiply_lower_tri_self_transpose(lkj_corr_cholesky_rng(K[c], eta[c]));
    real b = eta[c] - 1 + K[c] / 2.0;
    u_21[c] = beta_cdf((Omega[2, 1] + 1) / 2 | b, b);
    u_k1[c] = beta_cdf((Omega[K[c], 1] + 1) / 2 | b, b);
  }
}
