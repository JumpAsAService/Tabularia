data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real mu_a;
  real mu_b;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_a;
  real<lower=0.0> tau_b;
  matrix[2, J_grp] Z_eff;
  cholesky_factor_corr[2] L_eff;
}
transformed parameters {
  vector[J_grp] a_g;
  vector[J_grp] b_g;
  {
    matrix[2, J_grp] W_eff = diag_pre_multiply(to_vector([tau_a, tau_b]), L_eff) * Z_eff;
    a_g = mu_a + to_vector(W_eff[1]);
    b_g = mu_b + to_vector(W_eff[2]);
  }
}
model {
  mu_a ~ normal(0.0, 2.5);
  mu_b ~ normal(0.0, 2.5);
  sigma ~ exponential(1.0);
  tau_a ~ normal(0.0, 1.0);
  tau_b ~ normal(0.0, 1.0);
  to_vector(Z_eff) ~ std_normal();
  L_eff ~ lkj_corr_cholesky(2.0);
  y ~ normal(((a_g[g]) + ((b_g[g]) .* x)), sigma);
}
generated quantities {
  matrix[2, 2] rho_g = multiply_lower_tri_self_transpose(L_eff);
}
