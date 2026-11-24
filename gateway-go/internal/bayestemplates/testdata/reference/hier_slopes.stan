// Varying intercepts and slopes with an LKJ(2) correlation, the textbook non-centred form
// (Stan User's Guide, multivariate hierarchical priors).
data {
  int<lower=0> N;
  int<lower=1> J;
  array[N] int<lower=1, upper=J> g;
  vector[N] x;
  vector[N] y;
}
parameters {
  real mu_a;
  real mu_b;
  vector<lower=0>[2] tau;
  cholesky_factor_corr[2] L;
  matrix[2, J] z;
  real<lower=0> sigma;
}
transformed parameters {
  matrix[J, 2] eff = (diag_pre_multiply(tau, L) * z)';
  vector[J] a_g = mu_a + eff[, 1];
  vector[J] b_g = mu_b + eff[, 2];
  real tau_a = tau[1];
  real tau_b = tau[2];
}
model {
  mu_a ~ normal(0, 2.5);
  mu_b ~ normal(0, 2.5);
  tau ~ normal(0, 1);
  L ~ lkj_corr_cholesky(2);
  to_vector(z) ~ std_normal();
  sigma ~ exponential(1);
  for (n in 1:N)
    y[n] ~ normal(a_g[g[n]] + b_g[g[n]] * x[n], sigma);
}
generated quantities {
  matrix[2, 2] rho_g = multiply_lower_tri_self_transpose(L);
}
