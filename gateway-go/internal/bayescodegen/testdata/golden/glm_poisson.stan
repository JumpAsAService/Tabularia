data {
  int<lower=0> N;
  vector[N] x;
  array[N] int<lower=0> y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real alpha;
  real b_x;
}
model {
  vector[N] eta = (alpha + (b_x * x));
  alpha ~ normal(0.0, 2.5);
  b_x ~ normal(0.0, 2.5);
  y ~ poisson_log(eta);
}
