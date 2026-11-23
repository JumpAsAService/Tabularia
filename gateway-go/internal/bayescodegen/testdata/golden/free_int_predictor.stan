data {
  int<lower=0> N;
  array[N] int k;
  array[N] int<lower=0, upper=1> w;
  array[N] int<lower=0> y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real alpha;
  real b_k;
  real b_w;
}
model {
  vector[N] eta = ((alpha + (b_k * to_vector(k))) + (b_w * to_vector(w)));
  alpha ~ normal(0.0, 2.5);
  b_k ~ normal(0.0, 1.0);
  b_w ~ normal(0.0, 1.0);
  y ~ poisson_log(eta);
}
