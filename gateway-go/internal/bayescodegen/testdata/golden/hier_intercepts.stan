data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real b_x;
  real mu_a;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_a;
  vector<offset=mu_a, multiplier=tau_a>[J_grp] a_g;
}
model {
  vector[N] eta = ((a_g[g]) + (b_x * x));
  b_x ~ normal(0.0, 2.5);
  mu_a ~ normal(0.0, 2.5);
  sigma ~ exponential(1.0);
  tau_a ~ normal(0.0, 1.0);
  a_g ~ normal(mu_a, tau_a);
  y ~ normal(eta, sigma);
}
