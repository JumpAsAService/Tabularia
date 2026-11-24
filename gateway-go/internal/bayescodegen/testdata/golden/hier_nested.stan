data {
  int<lower=0> N;
  int<lower=1> J_dst;
  int<lower=1> J_sch;
  array[N] int<lower=1, upper=J_dst> d;
  array[N] int<lower=1, upper=J_sch> s;
  vector[N] x;
  vector[N] y;
  array[J_sch] int<lower=1, upper=J_dst> Up_d_sch;
  int<lower=0, upper=1> Mode;
}
parameters {
  real b_x;
  real mu;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_d;
  real<lower=0.0> tau_s;
  vector<offset=mu, multiplier=tau_d>[J_dst] u_d;
  vector<offset=(u_d[Up_d_sch]), multiplier=tau_s>[J_sch] u_s;
}
model {
  vector[N] eta = ((u_s[s]) + (b_x * x));
  b_x ~ normal(0.0, 2.5);
  mu ~ normal(0.0, 2.5);
  sigma ~ exponential(1.0);
  tau_d ~ normal(0.0, 1.0);
  tau_s ~ normal(0.0, 1.0);
  u_d ~ normal(mu, tau_d);
  u_s ~ normal((u_d[Up_d_sch]), tau_s);
  y ~ normal(eta, sigma);
}
