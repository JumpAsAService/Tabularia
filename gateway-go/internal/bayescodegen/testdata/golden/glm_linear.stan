data {
  int<lower=0> N;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real alpha;
  real b_x;
  real<lower=0.0> sigma;
}
model {
  vector[N] eta = (alpha + (b_x * x));
  alpha ~ normal(0.0, 2.5);
  b_x ~ normal(0.0, 2.5);
  sigma ~ exponential(1.0);
  y ~ normal(eta, sigma);
}
